import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class MeasureLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, ref, severity="serious"):
        return self.service.create_item(
            {"title": f"事故{ref}", "description": "事故描述", "severity": severity,
             "quantity": 5, "threshold": 10, "external_ref": ref},
            "creator", "reporter")

    def _to_verification(self, item):
        """把事故推到verification状态，便于验收退回核验。"""
        current = item
        for target in ["investigating", "corrective_action", "verification"]:
            current = self.service.transition(
                current["id"], target, current["version"],
                "reviewer", "safety_manager" if target == "verification" else "investigator")
        return current

    def test_create_measure_and_link_to_multiple_items(self):
        m = self.service.create_measure(
            {"measure_no": "CS-001", "title": "加装防护栏",
             "workstation": "冲压车间", "reason": "机械伤害"},
            "engineer", "safety_manager")
        self.assertEqual(m["measure_no"], "CS-001")
        self.assertEqual(m["status"], "active")
        self.assertEqual(m["acceptance"], "pending")
        self.assertTrue(m["is_valid"])

        i1 = self._item("ACC-1")
        i2 = self._item("ACC-2")
        m = self.service.link_measure(
            m["id"], {"item_id": i1["id"], "expected_version": m["version"]},
            "engineer", "investigator")
        m = self.service.link_measure(
            m["id"], {"item_id": i2["id"], "expected_version": m["version"]},
            "engineer", "investigator")
        detail = self.service.get_measure(m["id"], "viewer")
        self.assertEqual(len(detail["linked_items"]), 2)
        self.assertEqual({it["item_id"] for it in detail["linked_items"]},
                         {i1["id"], i2["id"]})

    def test_duplicate_measure_no_conflict(self):
        self.service.create_measure(
            {"measure_no": "CS-DUP", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        with self.assertRaises(ConflictError):
            self.service.create_measure(
                {"measure_no": "CS-DUP", "title": "重复", "workstation": "工位B",
                 "reason": "原因乙"}, "engineer", "safety_manager")

    def test_link_optimistic_lock_first_writer_wins(self):
        m = self.service.create_measure(
            {"measure_no": "CS-LOCK", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        i1 = self._item("ACC-L1")
        i2 = self._item("ACC-L2")
        # 先到者挂接成功
        self.service.link_measure(
            m["id"], {"item_id": i1["id"], "expected_version": m["version"]},
            "first", "investigator")
        # 后到者仍持旧版本 → 冲突，且看到当前版本
        with self.assertRaises(ConflictError) as ctx:
            self.service.link_measure(
                m["id"], {"item_id": i2["id"], "expected_version": m["version"]},
                "second", "investigator")
        self.assertIsNotNone(ctx.exception.current_version)
        self.assertGreater(ctx.exception.current_version, m["version"])
        # 后到者刷新后用当前版本挂接成功
        current = self.service.get_measure(m["id"], "viewer")
        self.service.link_measure(
            m["id"], {"item_id": i2["id"], "expected_version": current["version"]},
            "second", "investigator")
        detail = self.service.get_measure(m["id"], "viewer")
        self.assertEqual(len(detail["linked_items"]), 2)

    def test_acceptance_optimistic_lock_first_writer_wins(self):
        m = self.service.create_measure(
            {"measure_no": "CS-ACC", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        # 先到者验收通过
        m = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "首次验收"}, "first", "safety_manager")
        self.assertEqual(m["acceptance"], "accepted")
        # 后到者持旧版本验收 → 冲突，看到当前版本
        with self.assertRaises(ConflictError) as ctx:
            self.service.accept_measure(
                m["id"], {"conclusion": "rejected", "expected_version": 1,
                          "reason": "后到验收"}, "second", "safety_manager")
        self.assertEqual(ctx.exception.current_version, m["version"])

    def test_acceptance_change_invalidates_and_rolls_back_items(self):
        m = self.service.create_measure(
            {"measure_no": "CS-CHG", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        i1 = self._item("ACC-R1")
        i2 = self._item("ACC-R2")
        m = self.service.link_measure(
            m["id"], {"item_id": i1["id"], "expected_version": m["version"]},
            "engineer", "investigator")
        m = self.service.link_measure(
            m["id"], {"item_id": i2["id"], "expected_version": m["version"]},
            "engineer", "investigator")
        # 把两起事故推到verification
        v1 = self._to_verification(self.service.get_item(i1["id"], "viewer"))
        v2 = self._to_verification(self.service.get_item(i2["id"], "viewer"))
        # 首次验收通过
        m = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "首次验收通过"}, "engineer", "safety_manager")
        self.assertEqual(m["status"], "active")
        # 验收结论变化 → 失效重算
        m = self.service.accept_measure(
            m["id"], {"conclusion": "rejected", "expected_version": m["version"],
                      "reason": "防护栏强度不足，验收不通过"},
            "engineer", "safety_manager")
        self.assertEqual(m["status"], "invalid")
        self.assertEqual(m["acceptance"], "pending")
        self.assertFalse(m["is_valid"])
        # 引用事故退回核验
        for i in (v1, v2):
            rolled = self.service.get_item(i["id"], "viewer")
            self.assertEqual(rolled["status"], "verification")
        # 写明原因：事故下有measure_revoked记录
        records = self.service.list_records(v1["id"], "viewer")
        revoked = [r for r in records if r["kind"] == "measure_revoked"]
        self.assertEqual(len(revoked), 1)
        self.assertIn("强度不足", revoked[0]["detail"])
        # 审计可查
        events = self.service.audit("viewer", entity_type="measure")
        invalidated = [e for e in events if e["action"] == "measure_invalidated"]
        self.assertEqual(len(invalidated), 1)
        self.assertIn("强度不足", invalidated[0]["detail"]["reason"])
        self.assertEqual(set(invalidated[0]["detail"]["affected_items"]),
                         {v1["id"], v2["id"]})
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reacceptance_after_invalidation_reactivates(self):
        m = self.service.create_measure(
            {"measure_no": "CS-RE", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        i1 = self._item("ACC-RE1")
        m = self.service.link_measure(
            m["id"], {"item_id": i1["id"], "expected_version": m["version"]},
            "engineer", "investigator")
        self._to_verification(self.service.get_item(i1["id"], "viewer"))
        m = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "首次"}, "engineer", "safety_manager")
        m = self.service.accept_measure(
            m["id"], {"conclusion": "rejected", "expected_version": m["version"],
                      "reason": "不通过"}, "engineer", "safety_manager")
        self.assertEqual(m["status"], "invalid")
        # 失效后重算验收通过 → 恢复有效
        m = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "整改后重新验收通过"}, "engineer", "safety_manager")
        self.assertEqual(m["status"], "active")
        self.assertEqual(m["acceptance"], "accepted")

    def test_same_conclusion_is_idempotent(self):
        m = self.service.create_measure(
            {"measure_no": "CS-IDEM", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        m = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "首次"}, "engineer", "safety_manager")
        version_before = m["version"]
        m2 = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "重复提交"}, "engineer", "safety_manager")
        self.assertEqual(m2["version"], version_before)

    def test_acceptance_requires_reason(self):
        m = self.service.create_measure(
            {"measure_no": "CS-NR", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        m = self.service.accept_measure(
            m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                      "reason": "首次"}, "engineer", "safety_manager")
        with self.assertRaises(Exception):
            self.service.accept_measure(
                m["id"], {"conclusion": "rejected", "expected_version": m["version"],
                          "reason": "  "}, "engineer", "safety_manager")

    def test_migration_merges_by_workstation_and_reason(self):
        # 构造旧数据：每起事故各自建工单式措施记录（kind=action）
        i1 = self._item("MIG-1")
        i2 = self._item("MIG-2")
        i3 = self._item("MIG-3")
        # 工位A/原因甲：i1、i2 各一条 → 应归并为一条共用措施
        self.service.add_record(i1["id"], {
            "kind": "action", "detail": "加装防护栏", "status": "open",
            "external_ref": "A1", "workstation": "冲压车间", "reason": "机械伤害"},
            "recorder", "investigator")
        self.service.add_record(i2["id"], {
            "kind": "action", "detail": "加装防护栏", "status": "open",
            "external_ref": "A2", "workstation": "冲压车间", "reason": "机械伤害"},
            "recorder", "investigator")
        # 工位B/原因乙：i3 一条 → 另一条措施
        self.service.add_record(i3["id"], {
            "kind": "action", "detail": "更换劳保用品", "status": "open",
            "external_ref": "A3", "workstation": "焊接车间", "reason": "灼烫"},
            "recorder", "investigator")
        # 升级归并
        result = self.service.migrate_measures("admin", "safety_manager")
        self.assertEqual(result["created_count"], 2)
        by_key = {(c["workstation"], c["reason"]): c for c in result["created"]}
        self.assertIn(("冲压车间", "机械伤害"), by_key)
        self.assertIn(("焊接车间", "灼烫"), by_key)
        # 工位A/原因甲的措施挂到 i1、i2
        g1 = by_key[("冲压车间", "机械伤害")]
        self.assertEqual(set(g1["item_ids"]), {i1["id"], i2["id"]})
        self.assertEqual(g1["record_count"], 2)
        # 原明细可查
        detail = self.service.get_measure(g1["measure_id"], "viewer")
        self.assertEqual(len(detail["original_records"]), 2)
        self.assertEqual({r["item_id"] for r in detail["original_records"]},
                         {i1["id"], i2["id"]})
        # 审计可查
        events = self.service.audit("viewer", entity_type="measure")
        migrated = [e for e in events if e["action"] == "measure_created"
                   and e["detail"].get("migrated")]
        self.assertEqual(len(migrated), 2)
        summary = [e for e in events if e["action"] == "migrate_measures"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["detail"]["created"], 2)
        self.assertTrue(self.repo.verify_audit_chain())
        # 幂等：再次升级不重复归并
        again = self.service.migrate_measures("admin", "safety_manager")
        self.assertEqual(again["created_count"], 0)

    def test_migration_fallback_for_legacy_records_without_workstation(self):
        # 旧记录没有工位/原因（升级前的历史数据）
        i1 = self._item("LEG-1")
        self.service.add_record(i1["id"], {
            "kind": "action", "detail": "老旧整改措施", "status": "open",
            "external_ref": "L1"}, "recorder", "investigator")
        result = self.service.migrate_measures("admin", "safety_manager")
        self.assertEqual(result["created_count"], 1)
        created = result["created"][0]
        self.assertEqual(created["workstation"], "未指定工位")
        self.assertEqual(created["reason"], "未指定原因")

    def test_permission_denied_for_unauthorized_roles(self):
        m = self.service.create_measure(
            {"measure_no": "CS-PERM", "title": "措施", "workstation": "工位A",
             "reason": "原因甲"}, "engineer", "safety_manager")
        with self.assertRaises(PermissionDenied):
            self.service.accept_measure(
                m["id"], {"conclusion": "accepted", "expected_version": m["version"],
                          "reason": "越权"}, "attacker", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.migrate_measures("attacker", "viewer")


if __name__ == "__main__":
    unittest.main()
