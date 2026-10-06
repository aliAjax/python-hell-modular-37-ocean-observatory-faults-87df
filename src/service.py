import hashlib
import threading
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine

ACTIVE_INCIDENT_STATUSES = ("open", "diagnosing", "recovery_planned", "recovering")
ACTIVE_ACTION_STATUSES = ("proposed", "approved", "running")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self._replacement_lock = threading.Lock()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        if kind == "replacement":
            return self._create_replacement(actor, entity_id, status, payload, idempotency_key)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_replacement(self, actor, entity_id, status, payload, idempotency_key):
        old_asset = self.repository.get_entity(payload["old_asset_id"])
        payload["station_id"] = old_asset["data"].get("station_id")
        payload.setdefault("transferred_links", [])
        payload.setdefault("transferred_incidents", [])
        entity = self.repository.create_replacement_order(entity_id, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, entity["status"], {"kind": "replacement"})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        with self._replacement_lock:
            return self._attempt_handover(actor, entity, raise_if_blocked=False)

    def _handover_blockers(self, old_asset_id):
        incidents = [
            item
            for item in self.repository.list_entities("incident")
            if item["data"].get("asset_id") == old_asset_id and item["status"] in ACTIVE_INCIDENT_STATUSES
        ]
        incident_ids = {
            item["id"]
            for item in self.repository.list_entities("incident")
            if item["data"].get("asset_id") == old_asset_id
        }
        actions = [
            item
            for item in self.repository.list_entities("recovery_action")
            if item["status"] in ACTIVE_ACTION_STATUSES
            and (item["data"].get("asset_id") == old_asset_id or item["data"].get("incident_id") in incident_ids)
        ]
        return incidents, actions

    def _attempt_handover(self, actor, order, raise_if_blocked):
        """Transfer links and unclosed incidents to the replacement asset.

        Each transferred item is persisted before the next one starts, so a
        failed write leaves a resumable order and a retry only fills in what
        has not been transferred yet. Telemetry is never touched: historical
        and late high-revision data stay on the old asset.
        """
        if order["status"] != "pending_handover":
            return order
        data = dict(order["data"])
        old_asset_id = data.get("old_asset_id")
        new_asset_id = data.get("new_asset_id")
        incidents, actions = self._handover_blockers(old_asset_id)
        if incidents or actions:
            if raise_if_blocked:
                raise ConflictError(
                    "old asset still has active incidents or recovery actions: "
                    "incidents=%s recovery_actions=%s"
                    % ([item["id"] for item in incidents], [item["id"] for item in actions])
                )
            return order
        transferred_links = list(data.get("transferred_links", []))
        transferred_incidents = list(data.get("transferred_incidents", []))
        current = order
        for link in self.repository.list_entities("link"):
            if link["data"].get("asset_id") != old_asset_id or link["id"] in transferred_links:
                continue
            moved = dict(link["data"])
            moved["asset_id"] = new_asset_id
            self.repository.update_entity(link["id"], None, link["status"], moved)
            transferred_links.append(link["id"])
            data["transferred_links"] = list(transferred_links)
            current = self.repository.update_entity(current["id"], None, current["status"], data)
            self.audit.record(
                link["id"], actor, "replacement_transfer", link["status"], link["status"],
                {"replacement_id": order["id"], "from_asset_id": old_asset_id, "to_asset_id": new_asset_id},
            )
        for incident in self.repository.list_entities("incident"):
            if (
                incident["data"].get("asset_id") != old_asset_id
                or incident["status"] == "closed"
                or incident["id"] in transferred_incidents
            ):
                continue
            moved = dict(incident["data"])
            moved["asset_id"] = new_asset_id
            self.repository.update_entity(incident["id"], None, incident["status"], moved)
            transferred_incidents.append(incident["id"])
            data["transferred_incidents"] = list(transferred_incidents)
            current = self.repository.update_entity(current["id"], None, current["status"], data)
            self.audit.record(
                incident["id"], actor, "replacement_transfer", incident["status"], incident["status"],
                {"replacement_id": order["id"], "from_asset_id": old_asset_id, "to_asset_id": new_asset_id},
            )
        data["completed_at"] = utcnow()
        data["completed_by"] = actor.user_id
        completed = self.repository.update_entity(current["id"], None, "completed", data)
        self.audit.record(
            order["id"], actor, "execute", order["status"], "completed",
            {"transferred_links": transferred_links, "transferred_incidents": transferred_incidents},
        )
        return completed

    def _execute_replacement(self, actor, order, expected_version=None):
        self.rules.ensure_action_role(actor, "replacement", "execute")
        if order["status"] == "completed":
            return order
        with self._replacement_lock:
            fresh = self.repository.get_entity(order["id"])
            if fresh["status"] == "completed":
                return fresh
            if fresh["status"] != "pending_handover":
                raise InvalidTransition("cannot execute replacement from status " + fresh["status"])
            if expected_version is not None and int(expected_version) != fresh["version"]:
                raise ConflictError(
                    "version conflict: expected %s, found %s" % (expected_version, fresh["version"])
                )
            return self._attempt_handover(actor, fresh, raise_if_blocked=True)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "replacement" and action == "execute":
            return self._execute_replacement(actor, entity, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
