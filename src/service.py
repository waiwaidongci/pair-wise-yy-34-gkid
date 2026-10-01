from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (ACCEPT_MEASURE_ROLES, AUDIT_ROLES, CREATE_MEASURE_ROLES,
                    CREATE_ROLES, ENTITY, LINK_MEASURE_ROLES, MIGRATE_ROLES,
                    RECORD_ROLES, TITLE, VIEW_MEASURE_ROLES, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_acceptance, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        workstation = payload.get("workstation")
        if workstation is not None:
            workstation = require_text(workstation, "workstation", 100)
        reason = payload.get("reason")
        if reason is not None:
            reason = require_text(reason, "reason", 500)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, workstation, reason)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id, entity_type)

    # ------------------------------------------------------------------
    # 共用措施台账
    # ------------------------------------------------------------------
    def create_measure(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_MEASURE_ROLES)
        actor = require_text(actor, "actor", 100)
        measure_no = require_text(payload.get("measure_no"), "measure_no", 100)
        title = require_text(payload.get("title"), "title", 200)
        workstation = require_text(payload.get("workstation"), "workstation", 100)
        reason = require_text(payload.get("reason"), "reason", 500)
        measure = self.repository.create_measure(
            measure_no, title, workstation, reason, actor)
        return self._enrich_measure(measure)

    def link_measure(self, measure_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, LINK_MEASURE_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = payload.get("item_id")
        if not isinstance(item_id, int) or item_id < 1:
            raise ValidationError("item_id必须是正整数")
        expected_version = payload.get("expected_version")
        measure = self.repository.link_measure(
            measure_id, item_id, expected_version, actor)
        return self._enrich_measure(measure)

    def accept_measure(self, measure_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ACCEPT_MEASURE_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = validate_acceptance(payload.get("conclusion"))
        reason = require_text(payload.get("reason"), "reason", 500)
        expected_version = payload.get("expected_version")
        measure = self.repository.accept_measure(
            measure_id, conclusion, expected_version, reason, actor)
        return self._enrich_measure(measure)

    def list_measures(self, role: str, status: Optional[str] = None) -> list:
        ensure_role(role, VIEW_MEASURE_ROLES)
        return [self._enrich_measure(m) for m in self.repository.list_measures(status)]

    def get_measure(self, measure_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_MEASURE_ROLES)
        measure = self._enrich_measure(self.repository.get_measure(measure_id))
        measure["linked_items"] = self.repository.list_measure_items(measure_id)
        measure["original_records"] = self.repository.list_measure_records(measure_id)
        return measure

    def list_item_measures(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self._enrich_measure(m)
                for m in self.repository.list_item_measures(item_id)]

    def migrate_measures(self, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MIGRATE_ROLES)
        actor = require_text(actor, "actor", 100)
        return self.repository.migrate_to_shared_measures(actor)

    @staticmethod
    def _enrich_measure(measure: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(measure)
        result["is_valid"] = measure["status"] == "active"
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
