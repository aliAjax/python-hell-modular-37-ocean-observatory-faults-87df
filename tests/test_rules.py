import unittest

from src.domain import Actor, ConflictError, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.actor = Actor("tester", "admin")

    def test_telemetry_requires_increasing_revision(self):
        asset = {"id": "a-1", "kind": "asset", "status": "healthy", "data": {}}
        old = {"id": "t-1", "kind": "telemetry", "status": "current", "data": {"asset_id": "a-1", "metric": "pressure", "revision": 4}}
        lookup = lambda kind, field, value: [asset] if kind == "asset" else [old] if kind == "telemetry" else []
        with self.assertRaises(ConflictError):
            self.rules.validate_create(self.actor, "telemetry", {"asset_id": "a-1", "metric": "pressure", "value": 2, "observed_at": "2026-09-27", "revision": 3}, lookup)

    def test_asset_requires_station(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.actor, "asset", {"station_id": "missing", "asset_type": "sensor", "serial_no": "S", "last_seen": "2026-09-27"}, lambda k, f, v: [])

    def test_incident_requires_severity(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.actor, "incident", {"station_id": "s-1", "kind": "outage", "severity": "unknown", "summary": "x"}, lambda k, f, v: [])


if __name__ == "__main__":
    unittest.main()
