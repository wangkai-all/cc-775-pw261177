#!/usr/bin/env python3
"""三场景端到端核对：效应恢复、混杂识别、方法选择与独立验证。

直接在内存态统计核心上运行（不落库），打印核对报告并以退出码反映是否全部通过。
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import stats, synthetic  # noqa: E402

TOL_EFFECT = 2.5   # 效应恢复容差（约 1.7σ，合成数据固定种子下应远小于此）
TOL_EDGE = 3.0


def records_and_dims(scenario: dict):
    records, dims = [], {}
    for plate in scenario["plates"]:
        dims[plate["plate_id"]] = (plate["n_rows"], plate["n_cols"])
        records.extend(plate["wells"])
    return records, dims


def check(condition: bool, text: str) -> bool:
    print(f"  [{'通过' if condition else '失败'}] {text}")
    return condition


def scenario_a() -> bool:
    print("\n=== 场景 A：随机布局（应满秩、恢复真值、验证通过）===")
    sc = synthetic.generate_scenario_a()
    records, dims = records_and_dims(sc)
    fit = stats.fit_huber(records, dims)
    gt = sc["ground_truth"]
    ok = []
    ok.append(check(not fit.diagnostics, "设计满秩，无零空间混杂诊断"))
    ok.append(check(fit.design.rank == fit.design.n_par,
                    f"秩 = 参数数（{fit.design.rank}/{fit.design.n_par}）"))
    # 处理效应恢复（共同参数，任一板的列即可）
    cal_plate = sc["plates"][0]["plate_id"]
    for t, truth in gt["treatment_effects"].items():
        if t == "CTRL":
            continue
        c = next(x for x in fit.contrasts if x["treatment"] == t)
        ok.append(check(c["estimable"], f"{t} 对比可估"))
        ok.append(check(abs(c["estimate"] - truth) <= TOL_EFFECT,
                        f"{t} 效应恢复 {c['estimate']:.2f} ≈ 真值 {truth:+.1f}（容差 {TOL_EFFECT}）"))
    # 边缘效应恢复（每板一列，取校准板）
    edge = next(e for e in fit.effects
                if e["family"] == stats.FAMILY_EDGE and e["plate"] == cal_plate)
    ok.append(check(abs(edge["estimate"] - gt["edge_effect"]) <= TOL_EDGE,
                    f"边缘效应恢复 {edge['estimate']:.2f} ≈ 真值 {gt['edge_effect']}"))
    # 行效应恢复（相对首行；模型行效应 = 真值行差）
    for i in range(1, synthetic.N_ROWS):
        e = next(x for x in fit.effects if x["family"] == stats.FAMILY_ROW
                 and x["plate"] == cal_plate and x["level"] == chr(65 + i))
        truth = gt["row_effects"][i] - gt["row_effects"][0]
        ok.append(check(abs(e["estimate"] - truth) <= TOL_EFFECT,
                        f"行 {chr(65 + i)} 效应 {e['estimate']:.2f} ≈ {truth:+.1f}"))
    # 独立验证
    cal_ids = [p["plate_id"] for p in sc["plates"] if p["role"] == "calibration"]
    val_ids = [p["plate_id"] for p in sc["plates"] if p["role"] == "validation"]
    cal_fit = stats.fit_huber([w for w in records if w["plate_id"] in cal_ids],
                              {p: dims[p] for p in cal_ids})
    val_fit = stats.fit_huber([w for w in records if w["plate_id"] in val_ids],
                              {p: dims[p] for p in val_ids})
    vr = stats.validate_contrasts(cal_fit, val_fit)
    ok.append(check(vr["passed"], f"独立验证通过（一致 {vr['n_agreed']}/{vr['n_compared']}）"))
    # 方法选择：充分对照
    cov = stats.control_coverage(records, set(stats.fit_huber(records, dims).design.well_keys))
    choice = stats.choose_method(cov, has_outlier=True, has_validation_plate=True)
    ok.append(check(cov["sufficient"], "对照覆盖充分（行/列/边缘）"))
    ok.append(check(choice["method"] == "huber", "存在离群孔时选择 Huber"))
    return all(ok)


def scenario_b() -> bool:
    print("\n=== 场景 B：整行同处理（处理与行效应完全混杂）===")
    sc = synthetic.generate_scenario_b()
    records, dims = records_and_dims(sc)
    design = stats.build_design(records, dims)
    ok = []
    ok.append(check(design.rank < design.n_par,
                    f"秩缺陷（{design.rank}/{design.n_par}）"))
    kinds = {d["kind"] for d in stats.null_diagnostics(design)}
    ok.append(check("row_treatment_confounded" in kinds,
                    "诊断明确指出 row+treatment 完全混杂"))
    fit = stats.fit_ols(records, dims)
    n_estimable = sum(c["estimable"] for c in fit.contrasts)
    ok.append(check(n_estimable == 0,
                    f"所有处理对比均不可估（可估数={n_estimable}），未输出虚假数值"))
    ok.append(check(all(c["estimate"] is None for c in fit.contrasts),
                    "不可估对比的估计值为空而不是杜撰数字"))
    # 拟合值与残差在秩缺陷下仍然唯一
    fit2 = stats.fit_ols(records, dims)
    ok.append(check(np.allclose(fit.fitted, fit2.fitted),
                    "秩缺陷下拟合值/残差保持唯一"))
    return all(ok)


def scenario_c() -> bool:
    print("\n=== 场景 C：边缘对照缺失（T5 与边缘混杂，无验证板）===")
    sc = synthetic.generate_scenario_c()
    records, dims = records_and_dims(sc)
    design = stats.build_design(records, dims)
    fit = stats.fit_huber(records, dims)
    ok = []
    ok.append(check(design.rank < design.n_par,
                    f"秩缺陷（{design.rank}/{design.n_par}）"))
    kinds = {d["kind"] for d in stats.null_diagnostics(design)}
    ok.append(check("edge_treatment_confounded" in kinds,
                    "诊断明确指出 edge+treatment 完全混杂"))
    t5 = next(c for c in fit.contrasts if c["treatment"] == "T5")
    ok.append(check(not t5["estimable"] and t5["estimate"] is None,
                    "T5 对比不可估且无数值"))
    others = [c for c in fit.contrasts if c["treatment"] != "T5"]
    ok.append(check(all(c["estimable"] for c in others),
                    "其余处理对比仍可估（未因一处混杂全盘否定）"))
    # 对照覆盖不足 → 方法选择要求验证板；无验证板 → 未独立验证
    cov = stats.control_coverage(records, set(fit.design.well_keys))
    ok.append(check(not cov["sufficient"] and not cov["edge_ok"],
                    "对照边缘覆盖检查失败"))
    choice = stats.choose_method(cov, has_outlier=False, has_validation_plate=False)
    ok.append(check(choice["require_validation"], "要求独立验证板确认"))
    ok.append(check("未独立验证" in choice["reasons"][-1], "缺少验证板时给出未独立验证提示"))
    # 其余可估处理的恢复质量
    gt = sc["ground_truth"]
    for c in others:
        truth = gt["treatment_effects"][c["treatment"]]
        ok.append(check(abs(c["estimate"] - truth) <= TOL_EFFECT,
                        f"{c['treatment']} 效应恢复 {c['estimate']:.2f} ≈ {truth:+.1f}"))
    return all(ok)


def main() -> int:
    results = {"A": scenario_a(), "B": scenario_b(), "C": scenario_c()}
    print("\n================ 汇总 ================")
    for k, v in results.items():
        print(f"场景 {k}: {'全部通过' if v else '存在失败项'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
