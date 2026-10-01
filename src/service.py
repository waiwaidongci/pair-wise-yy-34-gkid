from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity, require_code,
                     require_conclusion, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    MEASURE_ACCEPT_ROLES, MEASURE_ATTACH_ROLES, MEASURE_CREATE_ROLES,
                    MEASURE_ENTITY, MEASURE_VERIFY_ROLES, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


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
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
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
        if target == "closed":
            blockers = blockers + self.repository.recheck_blockers(item_id)
        if blockers:
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

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 共用整改措施台账 ----
    @staticmethod
    def _expected_version(payload) -> int:
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        return expected

    def create_measure(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MEASURE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_code(payload.get("code"))
        title = require_text(payload.get("title"), "title", 200)
        workstation = require_text(payload.get("workstation"), "workstation", 200)
        reason = require_text(payload.get("reason"), "reason")
        measure = self.repository.create_measure(code, title, workstation, reason, actor)
        self.repository.append_audit("create_measure", MEASURE_ENTITY, measure["id"], actor, {
            "code": code, "title": title, "workstation": workstation, "reason": reason,
        })
        return self.measure_view(measure["id"], role)

    def list_measures(self, role: str, conclusion: Optional[str] = None) -> list:
        self._view(role)
        if conclusion is not None:
            require_conclusion(conclusion)
        return [self._measure_brief(m) for m in self.repository.list_measures(conclusion)]

    def get_measure(self, code: str, role: str) -> Dict[str, Any]:
        self._view(role)
        measure = self.repository.get_measure_by_code(code)
        return self.measure_view(measure["id"], role)

    def measure_view(self, measure_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        measure = self.repository.get_measure(measure_id)
        result = self._measure_brief(measure)
        result["links"] = []
        for link in self.repository.measure_links(measure_id):
            item = self.repository.get_item(link["item_id"])
            entry = dict(link)
            entry["item_status"] = item["status"]
            entry["stale"] = link["basis_version"] != measure["version"]
            result["links"].append(entry)
        result["sources"] = self.repository.measure_sources(measure_id)
        return result

    @staticmethod
    def _measure_brief(measure: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(measure)
        result["stale"] = False
        return result

    def attach_measure(self, code: str, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MEASURE_ATTACH_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = self._expected_version(payload)
        measure = self.repository.get_measure_by_code(code)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事故已关闭，不能挂接措施",
                                {"current_version": measure["version"]})
        link = self.repository.attach_measure(measure["id"], item_id, expected, actor)
        self.repository.append_audit("attach_measure", MEASURE_ENTITY, measure["id"], actor, {
            "code": code, "link_id": link["id"], "item_id": item_id,
            "basis_version": link["basis_version"],
        })
        result = dict(link)
        result["code"] = code
        return result

    def accept_measure(self, code: str, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, MEASURE_ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_conclusion(payload.get("conclusion"))
        expected = self._expected_version(payload)
        measure = self.repository.get_measure_by_code(code)
        updated = self.repository.accept_measure(
            measure["id"], conclusion, expected, actor)
        return self.measure_view(updated["id"], role)

    def verify_link(self, link_id: int, payload: Optional[Dict[str, Any]], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, MEASURE_VERIFY_ROLES)
        actor = require_text(actor, "actor", 100)
        payload = payload or {}
        reason = payload.get("recheck_reason")
        if reason is not None:
            reason = require_text(reason, "recheck_reason")
        link = self.repository.verify_link(link_id, actor, reason)
        self.repository.append_audit("verify_link", MEASURE_ENTITY,
                                     link["measure_id"], actor, {
            "link_id": link_id, "item_id": link["item_id"],
        })
        return link

    def list_item_measures(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_item_measures(item_id)

    def upgrade_legacy_measures(self) -> Dict[str, int]:
        return self.repository.migrate_legacy_measures()

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
