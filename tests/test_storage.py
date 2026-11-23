"""存储与快照不可变性测试。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analysis, storage, synthetic  # noqa: E402


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.conn = storage.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _import(self, scenario_key):
        func = {
            "A": synthetic.generate_scenario_a,
            "B": synthetic.generate_scenario_b,
            "C": synthetic.generate_scenario_c,
        }[scenario_key]
        sc = func()
        for plate in sc["plates"]:
            storage.import_plate(self.conn, plate)
        return sc

    def test_import_and_duplicate_rejection(self):
        sc = self._import("A")
        n_wells = sum(len(p["wells"]) for p in sc["plates"])
        self.assertEqual(len(storage.load_wells(self.conn)), n_wells)
        with self.assertRaises(ValueError):
            storage.import_plate(self.conn, sc["plates"][0])
        # 角色固定且只有两种合法值
        for p in storage.list_plates(self.conn):
            self.assertIn(p["role"], ("calibration", "validation"))

    def test_raw_wells_never_modified_by_analysis(self):
        sc = self._import("A")
        before = storage.load_wells(self.conn)
        ids = [p["plate_id"] for p in sc["plates"]]
        analysis.run_analysis(self.conn, ids)
        after = storage.load_wells(self.conn)
        self.assertEqual(before, after)  # 原始记录完全不变
        # wells 表没有任何坏孔列（标记只存在于分析快照）
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(wells)")}
        self.assertNotIn("bad_well", cols)
        self.assertNotIn("flagged", cols)

    def test_snapshot_immutable_when_new_plate_appended(self):
        sc = self._import("A")
        ids = [p["plate_id"] for p in sc["plates"]]
        aid1 = analysis.run_analysis(self.conn, ids, k=4.0)
        snap1 = storage.get_analysis(self.conn, aid1)
        frozen = json.dumps(snap1, sort_keys=True, ensure_ascii=False)

        # 追加场景 B 的板（模拟后续实验）
        self._import("B")
        all_ids = [p["plate_id"] for p in storage.list_plates(self.conn)]
        analysis.run_analysis(self.conn, all_ids, k=4.0)

        snap1_again = storage.get_analysis(self.conn, aid1)
        self.assertEqual(
            frozen, json.dumps(snap1_again, sort_keys=True, ensure_ascii=False))
        self.assertEqual(snap1_again["plate_set"], ids)
        self.assertEqual(len(storage.list_analyses(self.conn)), 2)

    def test_rerun_creates_version_and_keeps_parent(self):
        sc = self._import("A")
        ids = [p["plate_id"] for p in sc["plates"]]
        parent_id = analysis.run_analysis(self.conn, ids, k=4.0)
        parent = storage.get_analysis(self.conn, parent_id)
        # 找一个建议坏孔，显式豁免它（标记为好孔）
        key = parent["suggested_bad"][0]
        new_id = analysis.rerun_with_flags(
            self.conn, parent_id, {key: False}, note="测试豁免")
        self.assertNotEqual(parent_id, new_id)
        child = storage.get_analysis(self.conn, new_id)
        self.assertEqual(child["parent_analysis_id"], parent_id)
        self.assertEqual(child["plate_set"], parent["plate_set"])
        self.assertEqual(child["bad_well_k"], parent["bad_well_k"])
        self.assertIn(key, child["used_keys"])
        # 父版本不受影响
        parent_after = storage.get_analysis(self.conn, parent_id)
        self.assertNotIn(key, parent_after["used_keys"])
        self.assertEqual(
            parent_after["created_at"], parent["created_at"])

    def test_validation_result_frozen_in_snapshot(self):
        sc = self._import("A")
        ids = [p["plate_id"] for p in sc["plates"]]
        aid = analysis.run_analysis(self.conn, ids)
        snap = storage.get_analysis(self.conn, aid)
        self.assertIsNotNone(snap["validation_result"])
        self.assertTrue(snap["validation_result"]["passed"])

    def test_scenario_c_marks_unvalidated(self):
        sc = self._import("C")
        ids = [p["plate_id"] for p in sc["plates"]]
        aid = analysis.run_analysis(self.conn, ids)
        snap = storage.get_analysis(self.conn, aid)
        self.assertIn("未独立验证", snap["validation_result"]["note"])
        self.assertFalse(snap["validation_result"]["passed"])


if __name__ == "__main__":
    unittest.main()
