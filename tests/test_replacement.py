import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReplacementOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("engineer", "engineer")
        self.viewer = Actor("viewer", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def setup_assets(self):
        station = self.service.create(self.admin, "station", {"name": "S", "region": "R"})
        old = self.service.create(self.admin, "asset", {
            "station_id": station["id"], "asset_type": "sensor", "serial_no": "OLD",
            "last_seen": "2026-09-27T09:00:00Z",
        })
        new = self.service.create(self.admin, "asset", {
            "station_id": station["id"], "asset_type": "sensor", "serial_no": "NEW",
            "last_seen": "2026-09-27T09:00:00Z",
        })
        return station, old, new

    def replacement(self, old, new, effective_at="2026-09-27T10:00:00Z"):
        return self.service.create(self.admin, "replacement_order", {
            "old_asset_id": old["id"], "new_asset_id": new["id"], "effective_at": effective_at,
        })

    def test_links_and_unresolved_incidents_transfer_but_telemetry_stays(self):
        station, old, new = self.setup_assets()
        link = self.service.create(self.admin, "link", {
            "station_id": station["id"], "asset_id": old["id"], "link_type": "fiber", "capacity": 100,
        })
        incident = self.service.create(self.admin, "incident", {
            "station_id": station["id"], "asset_id": old["id"], "kind": "link_loss",
            "severity": "high", "summary": "no data",
        })
        telemetry = self.service.create(self.admin, "telemetry", {
            "asset_id": old["id"], "metric": "pressure", "value": 10,
            "observed_at": "2026-09-27T09:00:00Z", "revision": 1,
        })
        telemetry = self.service.transition(self.admin, telemetry["id"], "revise", {"value": 11, "revision": 3})

        order = self.replacement(old, new)
        self.assertEqual(order["status"], "completed")

        link = self.service.get(link["id"])
        self.assertEqual(link["data"]["asset_id"], new["id"])
        incident = self.service.get(incident["id"])
        self.assertEqual(incident["data"]["asset_id"], new["id"])
        self.assertIn(link["id"], order["data"]["transferred_links"])
        self.assertIn(incident["id"], order["data"]["transferred_incidents"])
        # historical telemetry and late high-revision data stay on the old asset
        telemetry = self.service.get(telemetry["id"])
        self.assertEqual(telemetry["data"]["asset_id"], old["id"])
        self.assertEqual(telemetry["data"]["revision"], 3)
        self.assertTrue(telemetry["data"]["late_revision"])

    def test_pending_handover_until_recovery_actions_clear(self):
        station, old, new = self.setup_assets()
        incident = self.service.create(self.admin, "incident", {
            "station_id": station["id"], "asset_id": old["id"], "kind": "loss",
            "severity": "high", "summary": "x",
        })
        action = self.service.create(self.admin, "recovery_action", {
            "incident_id": incident["id"], "asset_id": old["id"],
            "action_type": "remote_restart", "dedupe_key": "restart-1",
        })

        order = self.replacement(old, new)
        self.assertEqual(order["status"], "pending_handover")

        # resume while the action is still active: stays pending, nothing duplicated
        order = self.service.transition(self.admin, order["id"], "resume")
        self.assertEqual(order["status"], "pending_handover")

        # finish the recovery action, then resume completes the handover
        action = self.service.transition(self.admin, action["id"], "cancel")
        self.assertEqual(action["status"], "cancelled")
        order = self.service.transition(self.admin, order["id"], "resume")
        self.assertEqual(order["status"], "completed")

    def test_resume_only_fills_missing_transfers(self):
        station, old, new = self.setup_assets()
        incident = self.service.create(self.admin, "incident", {
            "station_id": station["id"], "asset_id": old["id"], "kind": "loss",
            "severity": "high", "summary": "x",
        })
        action = self.service.create(self.admin, "recovery_action", {
            "incident_id": incident["id"], "asset_id": old["id"],
            "action_type": "remote_restart", "dedupe_key": "restart-1",
        })
        link1 = self.service.create(self.admin, "link", {
            "station_id": station["id"], "asset_id": old["id"], "link_type": "fiber", "capacity": 100,
        })

        order = self.replacement(old, new)
        self.assertEqual(order["status"], "pending_handover")
        self.assertEqual(order["data"]["transferred_links"], [link1["id"]])

        # while pending, another link is added to the old asset
        link2 = self.service.create(self.admin, "link", {
            "station_id": station["id"], "asset_id": old["id"], "link_type": "copper", "capacity": 10,
        })

        # clearing the blocker and resuming picks up only the missing link
        action = self.service.transition(self.admin, action["id"], "cancel")
        order = self.service.transition(self.admin, order["id"], "resume")
        self.assertEqual(order["status"], "completed")
        self.assertEqual(sorted(order["data"]["transferred_links"]), sorted([link1["id"], link2["id"]]))
        for link_id in (link1["id"], link2["id"]):
            link = self.service.get(link_id)
            self.assertEqual(link["data"]["asset_id"], new["id"])

        # resuming again is rejected, so nothing transfers twice
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, order["id"], "resume")

    def test_duplicate_submission_does_not_transfer_twice(self):
        station, old, new = self.setup_assets()
        link = self.service.create(self.admin, "link", {
            "station_id": station["id"], "asset_id": old["id"], "link_type": "fiber", "capacity": 100,
        })
        first = self.service.create(self.admin, "replacement_order", {
            "old_asset_id": old["id"], "new_asset_id": new["id"], "effective_at": "2026-09-27T10:00:00Z",
        }, idempotency_key="idem-1")
        second = self.service.create(self.admin, "replacement_order", {
            "old_asset_id": old["id"], "new_asset_id": new["id"], "effective_at": "2026-09-27T10:00:00Z",
        }, idempotency_key="idem-1")
        self.assertEqual(first["id"], second["id"])
        link = self.service.get(link["id"])
        self.assertEqual(link["data"]["asset_id"], new["id"])

    def test_concurrent_registration_first_write_wins(self):
        station, old, new = self.setup_assets()
        attempts = []

        def register(actor, replacement):
            try:
                order = self.service.create(actor, "replacement_order", {
                    "old_asset_id": old["id"], "new_asset_id": replacement["id"],
                    "effective_at": "2026-09-27T10:00:00Z",
                })
                attempts.append(("ok", replacement["id"], order))
            except ConflictError as exc:
                attempts.append(("conflict", replacement["id"], str(exc)))

        other = self.service.create(self.admin, "asset", {
            "station_id": station["id"], "asset_type": "sensor", "serial_no": "NEW2",
            "last_seen": "2026-09-27T09:00:00Z",
        })
        t1 = threading.Thread(target=register, args=(self.admin, new))
        t2 = threading.Thread(target=register, args=(self.engineer, other))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        oks = [a for a in attempts if a[0] == "ok"]
        conflicts = [a for a in attempts if a[0] == "conflict"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(conflicts), 1)
        winner_asset = oks[0][1]
        loser_error = conflicts[0][2]
        # the loser sees who currently owns the asset
        self.assertIn(winner_asset, loser_error)
        self.assertIn(oks[0][2]["id"], loser_error)

    def test_engineer_can_void_unfinished_order(self):
        station, old, new = self.setup_assets()
        incident = self.service.create(self.admin, "incident", {
            "station_id": station["id"], "asset_id": old["id"], "kind": "loss",
            "severity": "high", "summary": "x",
        })
        action = self.service.create(self.admin, "recovery_action", {
            "incident_id": incident["id"], "asset_id": old["id"],
            "action_type": "remote_restart", "dedupe_key": "restart-1",
        })
        order = self.replacement(old, new)
        self.assertEqual(order["status"], "pending_handover")

        # a viewer cannot void
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.viewer, order["id"], "void")
        # the station engineer can void an unfinished order
        order = self.service.transition(self.engineer, order["id"], "void")
        self.assertEqual(order["status"], "void")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.engineer, order["id"], "void")

        # after voiding, the asset can be registered again
        action = self.service.transition(self.admin, action["id"], "cancel")
        order2 = self.replacement(old, new)
        self.assertEqual(order2["status"], "completed")

    def test_old_asset_must_exist_and_differ(self):
        station, old, new = self.setup_assets()
        with self.assertRaises(ValidationError):
            self.replacement(old, old)
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "replacement_order", {
                "old_asset_id": "missing", "new_asset_id": new["id"],
                "effective_at": "2026-09-27T10:00:00Z",
            })


if __name__ == "__main__":
    unittest.main()
