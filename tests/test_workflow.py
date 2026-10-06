import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def test_full_fault_recovery_flow(self):
        station = self.create("station", {"name": "OSN-01", "region": "East"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "S-1", "last_seen": "2026-09-27T09:00:00Z", "clock_offset_seconds": 0})
        link = self.create("link", {"station_id": station["id"], "asset_id": asset["id"], "link_type": "fiber", "capacity": 100})
        telemetry = self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 10, "observed_at": "2026-09-27T09:00:00Z", "revision": 1})
        telemetry = self.act(telemetry, "revise", {"value": 10.5, "revision": 2})
        self.assertTrue(telemetry["data"]["late_revision"])

        incident = self.create("incident", {"station_id": station["id"], "asset_id": asset["id"], "link_id": link["id"], "kind": "link_loss", "severity": "high", "summary": "no data"})
        incident = self.act(incident, "diagnose", {})
        incident = self.act(incident, "plan_recovery", {})
        incident = self.act(incident, "start_recovery", {})

        action = self.create("recovery_action", {"incident_id": incident["id"], "asset_id": asset["id"], "action_type": "remote_restart", "dedupe_key": "restart-1"})
        action = self.act(action, "approve", {})
        action = self.act(action, "start", {})
        action = self.act(action, "succeed", {"outcome": "asset online"})

        gap = self.create("gap", {"incident_id": incident["id"], "asset_id": asset["id"], "start_at": "2026-09-27T09:00:00Z", "end_at": "2026-09-27T09:30:00Z"})
        gap = self.act(gap, "estimate", {"estimate": "interpolation"})
        gap = self.act(gap, "fill", {"estimate": "interpolated series"})

        incident = self.act(incident, "resolve", {"summary": "service restored"})
        self.assertEqual(incident["status"], "resolved")
        incident = self.act(incident, "close", {})
        self.assertEqual(incident["status"], "closed")

    def test_late_revision_can_be_merged(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z"})
        telemetry = self.create("telemetry", {"asset_id": asset["id"], "metric": "temperature", "value": 1, "observed_at": "2026-09-27T09:00:00Z", "revision": 1})
        updated = self.service.transition(self.actor, telemetry["id"], "revise", {"value": 2, "revision": 3})
        self.assertEqual(updated["data"]["revision"], 3)


if __name__ == "__main__":
    unittest.main()
