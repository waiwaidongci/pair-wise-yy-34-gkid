import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


class MeasureLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, ref):
        return self.service.create_item({
            "title": ref, "description": "workshop injury", "severity": "moderate",
            "external_ref": ref,
        }, "creator", "reporter")

    def _measure(self, code="M-100"):
        return self.service.create_measure({
            "code": code, "title": "安装防护罩", "workstation": "冲压工位A",
            "reason": "设备缺少防护",
        }, "officer", "safety_manager")

    def _to_verification(self, item):
        current = item
        for target in STATES[1:4]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_one_measure_attached_to_multiple_accidents(self):
        measure = self._measure()
        item1 = self._item("OI-A")
        item2 = self._item("OI-B")
        link1 = self.service.attach_measure(measure["code"], item1["id"],
                                            {"expected_version": 1}, "inv", "investigator")
        self.assertEqual(link1["basis_version"], 1)
        self.assertEqual(self.service.get_measure(measure["code"], "viewer")["version"], 1)
        with self.assertRaises(ConflictError):
            self.service.attach_measure(measure["code"], item1["id"],
                                        {"expected_version": 1}, "inv", "investigator")
        self.service.attach_measure(measure["code"], item2["id"],
                                    {"expected_version": 1}, "inv", "investigator")
        view = self.service.get_measure(measure["code"], "viewer")
        self.assertEqual(len(view["links"]), 2)
        self.assertEqual(view["version"], 1)
        self.assertEqual(len(self.service.list_item_measures(item1["id"], "viewer")), 1)

    def test_acceptance_change_invalidates_and_returns_accidents(self):
        measure = self._measure()
        item1 = self._to_verification(self._item("OI-C"))
        item2 = self._to_verification(self._item("OI-D"))
        self.service.attach_measure(measure["code"], item1["id"],
                                    {"expected_version": 1}, "inv", "investigator")
        self.service.attach_measure(measure["code"], item2["id"],
                                    {"expected_version": 1}, "inv", "investigator")
        accepted = self.service.accept_measure(measure["code"], {
            "conclusion": "passed", "expected_version": 1}, "boss", "safety_manager")
        self.assertEqual(accepted["version"], 2)
        item1 = self.service.get_item(item1["id"], "viewer")
        self.assertEqual(item1["status"], "verification")
        links = {l["id"]: l for l in self.service.list_item_measures(item1["id"], "viewer")}
        link = next(iter(links.values()))
        self.assertEqual(link["link_state"], "unverified")
        self.service.verify_link(link["id"], {}, "boss", "safety_manager")
        self.service.transition(item1["id"], "closed", item1["version"], "boss",
                                "safety_manager")
        item1 = self.service.get_item(item1["id"], "viewer")
        self.assertEqual(item1["status"], "closed")
        result = self.service.accept_measure(measure["code"], {
            "conclusion": "failed", "expected_version": 2}, "boss", "safety_manager")
        self.assertEqual(result["version"], 3)
        item1 = self.service.get_item(item1["id"], "viewer")
        item2 = self.service.get_item(item2["id"], "viewer")
        self.assertEqual(item1["status"], "verification")
        view = self.service.get_measure(measure["code"], "viewer")
        link1 = next(l for l in view["links"] if l["item_id"] == item1["id"])
        self.assertEqual(link1["link_state"], "recheck")
        self.assertIn("验收结论", link1["recheck_reason"])
        records = self.service.list_records(item1["id"], "viewer")
        reasons = [r for r in records if r["kind"] == "recheck_reason" and r["status"] == "open"]
        self.assertTrue(reasons)
        self.assertTrue(any("failed" in r["detail"] for r in reasons))
        with self.assertRaises(ConflictError):
            self.service.transition(item1["id"], "closed", item1["version"], "boss",
                                    "safety_manager")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_concurrent_attach_first_writer_wins(self):
        measure = self._measure()
        item = self._item("OI-E")
        outcomes = []

        def attach():
            try:
                self.service.attach_measure(measure["code"], item["id"],
                                            {"expected_version": 1}, "inv",
                                            "investigator")
                outcomes.append("ok")
            except ConflictError as exc:
                outcomes.append(("conflict", exc.payload.get("current_version")))

        threads = [threading.Thread(target=attach) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 1)
        loser = [o for o in outcomes if o != "ok"][0]
        self.assertEqual(loser[0], "conflict")
        self.assertEqual(loser[1], 1)
        self.assertEqual(self.service.get_measure(measure["code"], "viewer")["version"], 1)

    def test_concurrent_attach_to_different_accidents_serializes(self):
        measure = self._measure("M-101")
        item_a = self._item("OI-E1")
        item_b = self._item("OI-E2")
        outcomes = []

        def attach(item, stale):
            try:
                self.service.attach_measure(measure["code"], item["id"],
                                            {"expected_version": 1}, "inv",
                                            "investigator")
                outcomes.append("ok")
            except ConflictError as exc:
                outcomes.append(("conflict", exc.payload.get("current_version")))

        t1 = threading.Thread(target=attach, args=(item_a, False))
        t2 = threading.Thread(target=attach, args=(item_b, True))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(outcomes.count("ok"), 2)
        view = self.service.get_measure(measure["code"], "viewer")
        self.assertEqual(len(view["links"]), 2)

    def test_concurrent_accept_first_writer_wins(self):
        measure = self._measure("M-102")
        item = self._item("OI-F")
        self.service.attach_measure(measure["code"], item["id"],
                                    {"expected_version": 1}, "inv", "investigator")
        outcomes = []

        def accept(conclusion):
            try:
                self.service.accept_measure(measure["code"], {
                    "conclusion": conclusion, "expected_version": 1},
                    "boss", "safety_manager")
                outcomes.append(("ok", conclusion))
            except ConflictError as exc:
                outcomes.append(("conflict", exc.payload.get("current_version")))

        t1 = threading.Thread(target=accept, args=("passed",))
        t2 = threading.Thread(target=accept, args=("failed",))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(oks), 1)
        loser = next(o for o in outcomes if o[0] == "conflict")
        self.assertEqual(loser[1], 2)
        self.assertEqual(self.service.get_measure(measure["code"], "viewer")["version"], 2)

    def test_legacy_records_merged_by_workstation_and_reason(self):
        item1 = self._item("LEG-1")
        item2 = self._item("LEG-2")
        detail1 = json.dumps({"workstation": "冲压工位A", "reason": "设备缺少防护",
                              "detail": "加装联锁防护罩"}, ensure_ascii=False)
        detail2 = json.dumps({"workstation": " 冲压工位A ", "reason": "设备缺少防护 ",
                              "detail": "同样问题"}, ensure_ascii=False)
        detail3 = json.dumps({"workstation": "焊接工位B", "reason": "通风不良",
                              "detail": "增加排风"}, ensure_ascii=False)
        self.service.add_record(item1["id"], {"kind": "corrective_action",
                                              "detail": detail1, "status": "closed"},
                                "inv", "investigator")
        self.service.add_record(item2["id"], {"kind": "corrective_action",
                                              "detail": detail2, "status": "closed"},
                                "inv", "investigator")
        self.service.add_record(item2["id"], {"kind": "corrective_action",
                                              "detail": detail3, "status": "open"},
                                "inv", "investigator")
        summary = self.service.upgrade_legacy_measures()
        self.assertEqual(summary["measures_created"], 2)
        self.assertEqual(summary["records_merged"], 3)
        measures = self.service.list_measures("viewer")
        by_ws = {m["workstation"]: m for m in measures}
        shared = by_ws["冲压工位A"]
        self.assertEqual(shared["conclusion"], "passed")
        view = self.service.get_measure(shared["code"], "viewer")
        self.assertEqual(len(view["sources"]), 2)
        self.assertEqual({s["record_id"] for s in view["sources"]},
                         {view["sources"][0]["record_id"], view["sources"][1]["record_id"]})
        linked_items = {l["item_id"] for l in view["links"]}
        self.assertEqual(linked_items, {item1["id"], item2["id"]})
        pending = by_ws["焊接工位B"]
        self.assertEqual(pending["conclusion"], "pending")
        self.assertEqual(
            len(self.service.list_records(item1["id"], "viewer")), 1)
        again = self.service.upgrade_legacy_measures()
        self.assertEqual(again["measures_created"], 0)
        codes = {m["code"] for m in self.service.list_measures("viewer")}
        self.assertEqual(len(codes), 2)
        events = self.service.audit("safety_manager")
        migrations = [e for e in events if e["action"] == "migrate"]
        self.assertEqual(len(migrations), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_roles_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_measure({"code": "X-1", "title": "t",
                                         "workstation": "w", "reason": "r"},
                                        "a", "reporter")
        self._measure("M-200")
        with self.assertRaises(PermissionDenied):
            self.service.accept_measure("M-200", {"conclusion": "passed",
                                                  "expected_version": 1},
                                        "a", "investigator")
        with self.assertRaises(ValidationError):
            self.service.create_measure({"code": "bad code!", "title": "t",
                                         "workstation": "w", "reason": "r"},
                                        "a", "safety_manager")
        with self.assertRaises(ValidationError):
            self.service.accept_measure("M-200", {"conclusion": "nope",
                                                  "expected_version": 1},
                                        "a", "safety_manager")


if __name__ == "__main__":
    unittest.main()
