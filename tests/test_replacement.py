import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FlakyRepository(SQLiteRepository):
    """Fails the Nth update_entity call to simulate a crashed write."""

    def __init__(self, path, fail_on_call):
        self.update_calls = 0
        self.fail_on_call = fail_on_call
        self.failures_enabled = True
        super().__init__(path)

    def update_entity(self, *args, **kwargs):
        self.update_calls += 1
        if self.failures_enabled and self.update_calls == self.fail_on_call:
            raise RuntimeError("simulated write failure")
        return super().update_entity(*args, **kwargs)


class ReplacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("site-eng", "engineer")

    def tearDown(self):
        self.tmp.cleanup()

    def make_station_assets(self):
        station = self.service.create(self.admin, "station", {"name": "OSN-02", "region": "South"})
        old = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "OLD-1", "last_seen": "2026-10-01T00:00:00Z"})
        new = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "NEW-1", "last_seen": "2026-10-01T00:00:00Z"})
        return station, old, new

    def register(self, old, new, actor=None, idem=None):
        data = {"old_asset_id": old["id"], "new_asset_id": new["id"], "effective_at": "2026-10-06T00:00:00Z"}
        return self.service.create(actor or self.admin, "replacement", data, idem)

    def open_incident(self, station, asset, kind="leak"):
        return self.service.create(self.admin, "incident", {"station_id": station["id"], "asset_id": asset["id"], "kind": kind, "severity": "high", "summary": kind})

    def resolve_incident(self, incident):
        if incident["status"] == "open":
            incident = self.service.transition(self.admin, incident["id"], "diagnose")
        if incident["status"] == "diagnosing":
            incident = self.service.transition(self.admin, incident["id"], "plan_recovery")
        if incident["status"] == "recovery_planned":
            incident = self.service.transition(self.admin, incident["id"], "start_recovery")
        return self.service.transition(self.admin, incident["id"], "resolve", {"summary": "fixed"})

    def test_handover_moves_links_and_unclosed_incidents_only(self):
        station, old, new = self.make_station_assets()
        link1 = self.service.create(self.admin, "link", {"station_id": station["id"], "asset_id": old["id"], "link_type": "fiber", "capacity": 100})
        link2 = self.service.create(self.admin, "link", {"station_id": station["id"], "asset_id": old["id"], "link_type": "acoustic", "capacity": 10})
        keeper = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "KEEP", "last_seen": "2026-10-01T00:00:00Z"})
        other_link = self.service.create(self.admin, "link", {"station_id": station["id"], "asset_id": keeper["id"], "link_type": "fiber", "capacity": 50})
        telemetry = self.service.create(self.admin, "telemetry", {"asset_id": old["id"], "metric": "pressure", "value": 8, "observed_at": "2026-10-02T00:00:00Z", "revision": 1})
        resolved = self.resolve_incident(self.open_incident(station, old, kind="drift"))
        closed = self.resolve_incident(self.open_incident(station, old, kind="noise"))
        closed = self.service.transition(self.admin, closed["id"], "close")

        order = self.register(old, new)
        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["data"]["completed_by"], "admin")
        self.assertEqual(set(order["data"]["transferred_links"]), {link1["id"], link2["id"]})
        self.assertEqual(order["data"]["transferred_incidents"], [resolved["id"]])

        self.assertEqual(self.service.get(link1["id"])["data"]["asset_id"], new["id"])
        self.assertEqual(self.service.get(link2["id"])["data"]["asset_id"], new["id"])
        self.assertEqual(self.service.get(other_link["id"])["data"]["asset_id"], keeper["id"])
        self.assertEqual(self.service.get(resolved["id"])["data"]["asset_id"], new["id"])
        self.assertEqual(self.service.get(closed["id"])["data"]["asset_id"], old["id"])

        # historical telemetry and late high-revision data stay on the old asset
        self.assertEqual(self.service.get(telemetry["id"])["data"]["asset_id"], old["id"])
        revised = self.service.transition(self.admin, telemetry["id"], "revise", {"value": 8.5, "revision": 2})
        self.assertEqual(revised["data"]["asset_id"], old["id"])

        audits = self.service.audit_log(link1["id"])
        self.assertEqual(audits[-1]["action"], "replacement_transfer")
        self.assertEqual(audits[-1]["detail"]["replacement_id"], order["id"])

    def test_active_fault_parks_order_in_pending_handover(self):
        station, old, new = self.make_station_assets()
        link = self.service.create(self.admin, "link", {"station_id": station["id"], "asset_id": old["id"], "link_type": "fiber", "capacity": 100})
        incident = self.open_incident(station, old)

        order = self.register(old, new)
        self.assertEqual(order["status"], "pending_handover")
        self.assertEqual(self.service.get(link["id"])["data"]["asset_id"], old["id"])

        with self.assertRaises(ConflictError):
            self.service.transition(self.engineer, order["id"], "execute")

        self.resolve_incident(incident)
        order = self.service.transition(self.engineer, order["id"], "execute")
        self.assertEqual(order["status"], "completed")
        self.assertEqual(self.service.get(link["id"])["data"]["asset_id"], new["id"])
        self.assertEqual(self.service.get(incident["id"])["data"]["asset_id"], new["id"])

    def test_active_recovery_action_parks_order(self):
        station, old, new = self.make_station_assets()
        incident = self.open_incident(station, old)
        for action in ("diagnose", "plan_recovery", "start_recovery"):
            incident = self.service.transition(self.admin, incident["id"], action)
        action = self.service.create(self.admin, "recovery_action", {"incident_id": incident["id"], "asset_id": old["id"], "action_type": "remote_restart", "dedupe_key": "rr-1"})

        order = self.register(old, new)
        self.assertEqual(order["status"], "pending_handover")
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, order["id"], "execute")

        self.service.transition(self.admin, action["id"], "cancel")
        self.resolve_incident(self.service.get(incident["id"]))
        order = self.service.transition(self.admin, order["id"], "execute")
        self.assertEqual(order["status"], "completed")

    def test_resume_after_write_failure_only_transfers_remaining(self):
        repo = FlakyRepository(Path(self.tmp.name) / "flaky.db", fail_on_call=3)
        service = DomainService(repo, RuleEngine())
        station = service.create(self.admin, "station", {"name": "S", "region": "R"})
        old = service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "O", "last_seen": "2026-10-01T00:00:00Z"})
        new = service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "N", "last_seen": "2026-10-01T00:00:00Z"})
        link1 = service.create(self.admin, "link", {"station_id": station["id"], "asset_id": old["id"], "link_type": "fiber", "capacity": 100})
        link2 = service.create(self.admin, "link", {"station_id": station["id"], "asset_id": old["id"], "link_type": "acoustic", "capacity": 10})

        data = {"old_asset_id": old["id"], "new_asset_id": new["id"], "effective_at": "2026-10-06T00:00:00Z"}
        with self.assertRaises(RuntimeError):
            service.create(self.admin, "replacement", data)

        order = service.list("replacement")[0]
        self.assertEqual(order["status"], "pending_handover")
        self.assertEqual(len(order["data"]["transferred_links"]), 1)
        moved_id = order["data"]["transferred_links"][0]
        stuck_id = link2["id"] if moved_id == link1["id"] else link1["id"]
        self.assertEqual(service.get(moved_id)["data"]["asset_id"], new["id"])
        self.assertEqual(service.get(stuck_id)["data"]["asset_id"], old["id"])

        repo.failures_enabled = False
        order = service.transition(self.admin, order["id"], "execute")
        self.assertEqual(order["status"], "completed")
        self.assertEqual(set(order["data"]["transferred_links"]), {link1["id"], link2["id"]})
        # the first link was not transferred twice
        self.assertEqual(service.get(moved_id)["version"], 2)
        self.assertEqual(service.get(stuck_id)["version"], 2)

        # a duplicate execute is a no-op and transfers nothing again
        again = service.transition(self.admin, order["id"], "execute")
        self.assertEqual(again["status"], "completed")
        self.assertEqual(service.get(link1["id"])["version"], 2)
        self.assertEqual(service.get(link2["id"])["version"], 2)

    def test_duplicate_submission_and_current_ownership(self):
        station, old, new = self.make_station_assets()
        self.open_incident(station, old)

        first = self.register(old, new, idem="swap-001")
        retry = self.register(old, new, idem="swap-001")
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(len(self.service.list("replacement")), 1)

        # a second person registering the same asset sees the current ownership
        with self.assertRaises(ConflictError) as ctx:
            self.register(old, new, actor=self.engineer, idem="swap-002")
        message = str(ctx.exception)
        self.assertIn(first["id"], message)
        self.assertIn("admin", message)
        self.assertEqual(len(self.service.list("replacement")), 1)

    def test_concurrent_registration_first_write_wins(self):
        station, old, new = self.make_station_assets()
        barrier = threading.Barrier(2)
        results = []

        def register(user):
            barrier.wait()
            try:
                order = self.register(old, new, actor=Actor(user, "engineer"))
                results.append(("ok", order["id"]))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))

        threads = [threading.Thread(target=register, args=("eng-a",)), threading.Thread(target=register, args=("eng-b",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        outcomes = sorted(result[0] for result in results)
        self.assertEqual(outcomes, ["conflict", "ok"])
        orders = self.service.list("replacement")
        self.assertEqual(len(orders), 1)
        conflict_message = next(message for outcome, message in results if outcome == "conflict")
        self.assertIn(orders[0]["id"], conflict_message)

    def test_completed_order_keeps_ownership_and_allows_chaining(self):
        station, old, new = self.make_station_assets()
        first = self.register(old, new)
        self.assertEqual(first["status"], "completed")

        with self.assertRaises(ConflictError):
            self.register(old, new, actor=self.engineer)

        # chaining stays possible: the replacement asset can be swapped out later
        third = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "THIRD", "last_seen": "2026-10-01T00:00:00Z"})
        chained = self.register(new, third)
        self.assertEqual(chained["status"], "completed")

    def test_void_permissions_and_claim_release(self):
        station, old, new = self.make_station_assets()
        self.open_incident(station, old)
        order = self.register(old, new)
        self.assertEqual(order["status"], "pending_handover")

        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("op", "operator"), order["id"], "void")

        order = self.service.transition(self.engineer, order["id"], "void")
        self.assertEqual(order["status"], "voided")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.engineer, order["id"], "void")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.engineer, order["id"], "execute")

        # the voided order releases its claim on the asset
        followup = self.register(old, new, actor=self.engineer)
        self.assertEqual(followup["status"], "pending_handover")

    def test_completed_order_cannot_be_voided(self):
        station, old, new = self.make_station_assets()
        order = self.register(old, new)
        self.assertEqual(order["status"], "completed")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.engineer, order["id"], "void")

    def test_execute_version_conflict(self):
        station, old, new = self.make_station_assets()
        self.open_incident(station, old)
        order = self.register(old, new)
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, order["id"], "execute", {}, 999)

    def test_cross_station_same_asset_and_role_rejected(self):
        station, old, new = self.make_station_assets()
        other_station = self.service.create(self.admin, "station", {"name": "OSN-09", "region": "North"})
        outsider = self.service.create(self.admin, "asset", {"station_id": other_station["id"], "asset_type": "sensor", "serial_no": "OUT", "last_seen": "2026-10-01T00:00:00Z"})
        with self.assertRaises(ValidationError):
            self.register(old, outsider)
        with self.assertRaises(ValidationError):
            self.register(old, old)
        with self.assertRaises(PermissionDenied):
            self.register(old, new, actor=Actor("viewer", "viewer"))


if __name__ == "__main__":
    unittest.main()
