"""统计核心测试：秩、可估性、稳健性、坏孔语义与方法选择。"""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import stats, synthetic  # noqa: E402


def _flatten(scenario):
    records, dims = [], {}
    for plate in scenario["plates"]:
        dims[plate["plate_id"]] = (plate["n_rows"], plate["n_cols"])
        records.extend(plate["wells"])
    return records, dims


class DesignRankTests(unittest.TestCase):
    def test_scenario_a_full_rank(self):
        records, dims = _flatten(synthetic.generate_scenario_a())
        design = stats.build_design(records, dims)
        self.assertEqual(design.rank, design.n_par)
        self.assertEqual(design.V0.shape[1], 0)
        self.assertEqual(stats.null_diagnostics(design), [])

    def test_scenario_b_row_treatment_confounded(self):
        records, dims = _flatten(synthetic.generate_scenario_b())
        design = stats.build_design(records, dims)
        self.assertLess(design.rank, design.n_par)
        kinds = {d["kind"] for d in stats.null_diagnostics(design)}
        self.assertIn("row_treatment_confounded", kinds)
        fit = stats.fit_ols(records, dims)
        self.assertTrue(all(not c["estimable"] for c in fit.contrasts))
        self.assertTrue(all(c["estimate"] is None for c in fit.contrasts))
        self.assertTrue(all(c["note"] for c in fit.contrasts))

    def test_scenario_c_edge_treatment_confounded_only_t5(self):
        records, dims = _flatten(synthetic.generate_scenario_c())
        design = stats.build_design(records, dims)
        self.assertLess(design.rank, design.n_par)
        kinds = {d["kind"] for d in stats.null_diagnostics(design)}
        self.assertIn("edge_treatment_confounded", kinds)
        fit = stats.fit_huber(records, dims)
        t5 = next(c for c in fit.contrasts if c["treatment"] == "T5")
        self.assertFalse(t5["estimable"])
        self.assertIsNone(t5["estimate"])
        self.assertTrue(all(c["estimable"]
                            for c in fit.contrasts if c["treatment"] != "T5"))

    def test_fitted_values_unique_under_rank_deficiency(self):
        records, dims = _flatten(synthetic.generate_scenario_b())
        f1 = stats.fit_ols(records, dims)
        f2 = stats.fit_ols(records, dims)
        np.testing.assert_allclose(f1.fitted, f2.fitted)
        np.testing.assert_allclose(f1.residuals, f2.residuals)


class RecoveryTests(unittest.TestCase):
    def test_scenario_a_recovers_truth(self):
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        fit = stats.fit_huber(records, dims)
        gt = sc["ground_truth"]
        for c in fit.contrasts:
            self.assertAlmostEqual(c["estimate"],
                                   gt["treatment_effects"][c["treatment"]],
                                   delta=2.5)
        edge = next(e for e in fit.effects
                    if e["family"] == stats.FAMILY_EDGE
                    and e["plate"] == sc["plates"][0]["plate_id"])
        self.assertAlmostEqual(edge["estimate"], gt["edge_effect"], delta=3.0)

    def test_validation_agreement(self):
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        cal = [p["plate_id"] for p in sc["plates"] if p["role"] == "calibration"]
        val = [p["plate_id"] for p in sc["plates"] if p["role"] == "validation"]
        f_cal = stats.fit_huber([w for w in records if w["plate_id"] in cal],
                                {p: dims[p] for p in cal})
        f_val = stats.fit_huber([w for w in records if w["plate_id"] in val],
                                {p: dims[p] for p in val})
        result = stats.validate_contrasts(f_cal, f_val)
        self.assertTrue(result["passed"])
        self.assertEqual(result["n_compared"], 5)

    def test_validation_catches_real_disagreement(self):
        # 人为给验证板 T3 加 5 单位真实偏移，验证必须判失败（准则不能过松）
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        cal = [p["plate_id"] for p in sc["plates"] if p["role"] == "calibration"]
        val = [p["plate_id"] for p in sc["plates"] if p["role"] == "validation"]
        shifted = [dict(w, response=w["response"] + (5.0 if w["treatment"] == "T3" else 0.0))
                   for w in records if w["plate_id"] in val]
        f_cal = stats.fit_huber([w for w in records if w["plate_id"] in cal],
                                {p: dims[p] for p in cal})
        f_val = stats.fit_huber(shifted, {p: dims[p] for p in val})
        result = stats.validate_contrasts(f_cal, f_val)
        self.assertFalse(result["passed"])
        t3 = next(r for r in result["rows"] if r["treatment"] == "T3")
        self.assertEqual(t3["status"], "disagree")
        self.assertGreater(abs(t3["difference"]), t3["se_difference"])

    def test_synthetic_is_deterministic(self):
        a1 = synthetic.generate_scenario_a()
        a2 = synthetic.generate_scenario_a()
        self.assertEqual(
            [w["response"] for p in a1["plates"] for w in p["wells"]],
            [w["response"] for p in a2["plates"] for w in p["wells"]])


class BadWellTests(unittest.TestCase):
    def test_suggestions_flag_outliers_and_exclusion_keeps_records(self):
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        probe = stats.fit_huber(records, dims)
        suggested = set(stats.suggest_bad_wells(probe, k=4.0))
        # 合成时注入的离群孔（每板 3 个）应基本被建议命中
        truth_bad = {f"{w['plate_id']}|{w['row_idx']}|{w['col_idx']}"
                     for p in sc["plates"] for w in p["wells"]
                     if w["is_outlier_truth"]}
        self.assertGreaterEqual(len(suggested & truth_bad),
                                math.floor(0.8 * len(truth_bad)))
        # 屏蔽后重拟合：原记录一条不少，被屏蔽孔仍有拟合值
        n_before = len(records)
        used = set(probe.design.well_keys) - suggested
        fit = stats.fit_huber(records, dims, used_keys=used)
        self.assertEqual(len(records), n_before)
        self.assertEqual(len(fit.fitted), n_before)
        excluded_idx = [i for i, k in enumerate(fit.design.well_keys)
                        if k in suggested]
        self.assertTrue(all(not fit.used_mask[i] for i in excluded_idx))
        self.assertTrue(all(np.isfinite(fit.fitted[i]) for i in excluded_idx))

    def test_huber_beats_ols_under_outlier(self):
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        # 再加一批极端离群
        for w in records[:: 37]:
            w["response"] += 40.0
        gt = sc["ground_truth"]
        ols = stats.fit_ols(records, dims)
        hub = stats.fit_huber(records, dims)
        t = "T3"
        ols_err = abs(next(c for c in ols.contrasts if c["treatment"] == t)["estimate"]
                      - gt["treatment_effects"][t])
        hub_err = abs(next(c for c in hub.contrasts if c["treatment"] == t)["estimate"]
                      - gt["treatment_effects"][t])
        self.assertLess(hub_err, ols_err)


class MethodChoiceTests(unittest.TestCase):
    def test_sufficient_coverage_picks_ols_or_huber(self):
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        fit = stats.fit_huber(records, dims)
        cov = stats.control_coverage(records, set(fit.design.well_keys))
        self.assertTrue(cov["sufficient"])
        self.assertEqual(
            stats.choose_method(cov, has_outlier=False,
                                has_validation_plate=True)["method"], "ols")
        self.assertEqual(
            stats.choose_method(cov, has_outlier=True,
                                has_validation_plate=True)["method"], "huber")

    def test_insufficient_coverage_requires_validation(self):
        sc = synthetic.generate_scenario_c()
        records, dims = _flatten(sc)
        fit = stats.fit_huber(records, dims)
        cov = stats.control_coverage(records, set(fit.design.well_keys))
        self.assertFalse(cov["sufficient"])
        choice = stats.choose_method(cov, False, False)
        self.assertTrue(choice["require_validation"])
        self.assertTrue(any("未独立验证" in r for r in choice["reasons"]))


class MedianPolishTests(unittest.TestCase):
    def test_perfect_additive_recovery(self):
        mat = np.zeros((4, 5))
        r = np.array([1.0, -2.0, 3.0, 0.5])
        c = np.array([0.0, 1.5, -1.0, 2.0, -0.5])
        mat = 10.0 + r[:, None] + c[None, :]
        mp = stats.median_polish(mat)
        np.testing.assert_allclose(mp["remainder"], 0.0, atol=1e-8)
        np.testing.assert_allclose(
            mp["overall"] + np.array(mp["row"])[:, None]
            + np.array(mp["col"])[None, :], mat, atol=1e-8)

    def test_pairwise_se_finite_and_positive(self):
        sc = synthetic.generate_scenario_a()
        records, dims = _flatten(sc)
        fit = stats.fit_huber(records, dims)
        self.assertTrue(all(c["se"] > 0 and math.isfinite(c["se"])
                            for c in fit.pairwise if c["estimable"]))
        self.assertTrue(all(c["ci_low"] < c["estimate"] < c["ci_high"]
                            for c in fit.pairwise if c["estimable"]))


if __name__ == "__main__":
    unittest.main()
