"""HTTP API 冒烟测试：服务在临时库、临时端口上运行。"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import server, storage  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "api.db")
        cls.httpd = server.make_server("127.0.0.1", 0, cls.db)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_01_health_and_static(self):
        status, data = self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertIn("实验板", resp.read().decode())
        with urllib.request.urlopen(self.base + "/static/app.js", timeout=10) as resp:
            self.assertEqual(resp.status, 200)

    def test_02_import_scenarios(self):
        status, data = self._request(
            "/api/import/scenario", "POST", {"scenario": "A_random"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["imported"]), 3)
        # 重复导入被拒绝且不会部分写入
        status2, data2 = self._request(
            "/api/import/scenario", "POST", {"scenario": "A_random"})
        self.assertEqual(status2, 409)
        self.assertIn("已存在", data2["error"])
        self._request("/api/import/scenario", "POST",
                      {"scenario": "B_row_confounded"})
        self._request("/api/import/scenario", "POST",
                      {"scenario": "C_edge_missing"})
        status, plates = self._request("/api/plates")
        self.assertEqual(status, 200)
        self.assertEqual(len(plates), 8)
        # 角色固定：A-VAL-1 是验证板
        val = next(p for p in plates if p["plate_id"] == "A-VAL-1")
        self.assertEqual(val["role"], "validation")

    def test_03_analysis_full_flow(self):
        status, plates = self._request("/api/plates")
        a_plates = [p["plate_id"] for p in plates
                    if p["scenario"] == "A_random"]
        status, data = self._request(
            "/api/analyses", "POST", {"plate_ids": a_plates, "k": 4.0})
        self.assertEqual(status, 201)
        aid = data["analysis_id"]
        status, snap = self._request(f"/api/analyses/{aid}")
        self.assertEqual(status, 200)
        self.assertFalse(snap["rank_deficient"])
        self.assertTrue(snap["validation_result"]["passed"])
        self.assertTrue(snap["wells"])
        # 原始板接口不含坏孔字段
        status, plate = self._request(f"/api/plates/{a_plates[0]}")
        self.assertNotIn("bad_well", plate["wells"][0])

    def test_04_confounded_analysis_flags_unestimable(self):
        status, plates = self._request("/api/plates")
        b_plates = [p["plate_id"] for p in plates
                    if p["scenario"] == "B_row_confounded"]
        status, data = self._request(
            "/api/analyses", "POST", {"plate_ids": b_plates})
        snap_id = data["analysis_id"]
        _, snap = self._request(f"/api/analyses/{snap_id}")
        self.assertTrue(snap["rank_deficient"])
        self.assertTrue(any(d["kind"] == "row_treatment_confounded"
                            for d in snap["diagnostics"]))
        treat_rows = [c for c in snap["contrasts"] if c["kind"] == "treatment"]
        self.assertTrue(all(not c["estimable"] for c in treat_rows))
        self.assertTrue(all(c["estimate"] is None for c in treat_rows))

    def test_05_rerun_versioning(self):
        _, analyses = self._request("/api/analyses")
        parent_id = analyses[0]["id"]
        _, parent = self._request(f"/api/analyses/{parent_id}")
        if not parent["suggested_bad"]:
            self.skipTest("该分析没有坏孔建议，无法测试翻转")
        key = parent["suggested_bad"][0]
        status, data = self._request(
            f"/api/analyses/{parent_id}/rerun", "POST",
            {"explicit_flags": {key: False}, "note": "API 翻转"})
        self.assertEqual(status, 201)
        _, child = self._request(f"/api/analyses/{data['analysis_id']}")
        self.assertEqual(child["parent_analysis_id"], parent_id)
        self.assertIn(key, child["used_keys"])
        # 父版本仍旧
        _, parent_again = self._request(f"/api/analyses/{parent_id}")
        self.assertNotIn(key, parent_again["used_keys"])

    def test_06_unknown_routes_and_bad_input(self):
        status, _ = self._request("/api/nope")
        self.assertEqual(status, 404)
        status, _ = self._request("/api/analyses", "POST",
                                  {"plate_ids": ["NO-SUCH-PLATE"]})
        self.assertEqual(status, 400)
        status, _ = self._request(
            "/api/import/scenario", "POST", {"scenario": "BOGUS"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
