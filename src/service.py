import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "replacement_order":
            return self._create_replacement(actor, dict(data or {}), idempotency_key)
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
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "replacement_order":
            if action == "resume":
                return self._resume_replacement(actor, entity, expected_version)
            if action == "void":
                return self._void_replacement(actor, entity, expected_version)
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

    def _find_active_replacement(self, old_asset_id):
        for order in self.repository.list_entities(kind="replacement_order"):
            if order["status"] != "void" and order["data"].get("old_asset_id") == old_asset_id:
                return order
        return None

    def _create_replacement(self, actor, payload, idempotency_key):
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, "replacement_order", payload, self._lookup)
        old_id = payload["old_asset_id"]
        new_id = payload["new_asset_id"]
        if old_id == new_id:
            raise ValidationError("replacement asset must differ from old asset")
        old = self.repository.get_entity(old_id)
        if not old or old["kind"] != "asset":
            raise ValidationError("old asset not found")
        new = self.repository.get_entity(new_id)
        if not new or new["kind"] != "asset":
            raise ValidationError("replacement asset not found")
        existing_order = self._find_active_replacement(old_id)
        if existing_order:
            raise ConflictError(
                "asset %s already has replacement order %s; current owner is %s"
                % (old_id, existing_order["id"], existing_order["data"].get("new_asset_id"))
            )
        entity_id = str(uuid4())
        order_data = {
            "old_asset_id": old_id,
            "new_asset_id": new_id,
            "effective_at": payload.get("effective_at"),
            "transferred_links": [],
            "transferred_incidents": [],
        }
        try:
            order = self.repository.create_entity(
                entity_id, "replacement_order", "pending_handover", order_data, actor.user_id
            )
        except ConflictError:
            # Two concurrent registrations can both pass the pre-check; the
            # database unique index then rejects the loser. Report the current
            # owner so the later caller sees who owns the asset now.
            existing_order = self._find_active_replacement(old_id)
            if existing_order:
                raise ConflictError(
                    "asset %s already has replacement order %s; current owner is %s"
                    % (old_id, existing_order["id"], existing_order["data"].get("new_asset_id"))
                )
            raise
        order = self._perform_handover(order)
        final_status = "pending_handover" if self._handover_blocked(old_id) else "completed"
        order = self.repository.update_entity(entity_id, order["version"], final_status, order["data"])
        self.audit.record(
            entity_id, actor, "create", None, final_status,
            {
                "old_asset_id": old_id,
                "new_asset_id": new_id,
                "effective_at": payload.get("effective_at"),
                "transferred_links": order["data"]["transferred_links"],
                "transferred_incidents": order["data"]["transferred_incidents"],
            },
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return order

    def _perform_handover(self, order):
        """Move links and unresolved incidents to the replacement asset.

        Only entities still pointing at the old asset are moved, and every
        moved id is recorded on the order, so a retry only fills in what a
        previous attempt did not transfer.
        """
        data = dict(order["data"])
        old_id = data["old_asset_id"]
        new_id = data["new_asset_id"]
        transferred_links = list(data.get("transferred_links", []))
        transferred_incidents = list(data.get("transferred_incidents", []))
        for link_id in self.repository.reassign_asset("link", old_id, new_id):
            if link_id not in transferred_links:
                transferred_links.append(link_id)
        for incident_id in self.repository.reassign_asset(
            "incident", old_id, new_id, exclude_statuses=("resolved", "closed")
        ):
            if incident_id not in transferred_incidents:
                transferred_incidents.append(incident_id)
        data["transferred_links"] = transferred_links
        data["transferred_incidents"] = transferred_incidents
        return self.repository.update_entity(order["id"], order["version"], order["status"], data)

    def _handover_blocked(self, old_asset_id):
        active_incident = ("open", "diagnosing", "recovery_planned", "recovering")
        active_action = ("proposed", "approved", "running")
        for incident in self.repository.list_entities(kind="incident"):
            if incident["data"].get("asset_id") == old_asset_id and incident["status"] in active_incident:
                return True
        for action in self.repository.list_entities(kind="recovery_action"):
            if action["status"] not in active_action:
                continue
            if action["data"].get("asset_id") == old_asset_id:
                return True
            incident = self.repository.get_entity(action["data"].get("incident_id"))
            if incident and incident["data"].get("asset_id") == old_asset_id:
                return True
        return False

    def _resume_replacement(self, actor, entity, expected_version=None):
        self.rules.validate_transition(actor, entity, "resume", {}, self._lookup)
        order = self._perform_handover(entity)
        final_status = "pending_handover" if self._handover_blocked(order["data"]["old_asset_id"]) else "completed"
        order = self.repository.update_entity(order["id"], order["version"], final_status, order["data"])
        self.audit.record(
            order["id"], actor, "resume", entity["status"], final_status,
            {
                "transferred_links": order["data"]["transferred_links"],
                "transferred_incidents": order["data"]["transferred_incidents"],
            },
        )
        return order

    def _void_replacement(self, actor, entity, expected_version=None):
        self.rules.validate_transition(actor, entity, "void", {}, self._lookup)
        order = self.repository.update_entity(entity["id"], entity["version"], "void", entity["data"])
        self.audit.record(entity["id"], actor, "void", entity["status"], "void", {})
        return order
