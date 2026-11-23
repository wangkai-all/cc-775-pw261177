"""分析编排：拟合、坏孔建议、方法选择、独立验证、快照落库。

关键约束（DESIGN.md）：
* 坏孔只屏蔽不删除，原记录保留；
* 每次分析创建不可变快照；翻转坏孔标记 → 新版本，旧版本不动；
* 追加板后旧分析不会被重算（本模块对给定 plate_set 一次性建快照）。
"""
from __future__ import annotations

import math

from . import stats, storage

FITTERS = {"ols": stats.fit_ols, "huber": stats.fit_huber}


def _all_keys(wells: list[dict]) -> set[str]:
    return {f"{w['plate_id']}|{w['row_idx']}|{w['col_idx']}" for w in wells}


def _well_rows(wells: list[dict], fit: stats.FitResult,
               suggested: set[str], used: set[str]) -> list[dict]:
    rows = []
    rstd_by_key = dict(zip(fit.design.well_keys, fit.rstd))
    for w in wells:
        key = f"{w['plate_id']}|{w['row_idx']}|{w['col_idx']}"
        idx = fit.design.well_keys.index(key)
        rstd = rstd_by_key.get(key)
        rows.append({
            "plate_id": w["plate_id"], "row_idx": w["row_idx"],
            "col_idx": w["col_idx"], "treatment": w["treatment"],
            "edge": w["edge"],
            "replicate_group": w.get("replicate_group", ""),
            "response": w["response"],
            "fitted": float(fit.fitted[idx]),
            "residual": float(fit.residuals[idx]),
            "rstd": (None if rstd is None or math.isnan(rstd) else float(rstd)),
            "used_in_fit": key in used,
            "suggested_bad": key in suggested,
            "flagged_bad": key not in used,
        })
    return rows


def _contrast_rows(fit: stats.FitResult, mp_result: dict | None) -> list[dict]:
    rows = []
    nonestimable_treatments = {c["treatment"] for c in fit.contrasts
                               if not c["estimable"]}
    for c in fit.contrasts:
        rows.append({"kind": "treatment", "label": f"{c['treatment']} vs {c['vs']}",
                     "estimable": c["estimable"], "estimate": c.get("estimate"),
                     "se": c.get("se"), "ci_low": c.get("ci_low"),
                     "ci_high": c.get("ci_high"), "note": c.get("note"),
                     "extra": {"treatment": c["treatment"], "vs": c["vs"]}})
    for c in fit.pairwise:
        rows.append({"kind": "pairwise", "label": f"{c['a']} − {c['b']}",
                     "estimable": c["estimable"], "estimate": c.get("estimate"),
                     "se": c.get("se"), "ci_low": c.get("ci_low"),
                     "ci_high": c.get("ci_high"), "note": c.get("note"),
                     "extra": {"a": c["a"], "b": c["b"]}})
    if mp_result:
        for c in mp_result["contrasts"]:
            confounded = c["treatment"] in nonestimable_treatments
            rows.append({
                "kind": "median_polish",
                "label": f"{c['treatment']} vs {mp_result['reference']}（中位数抛光）",
                "estimable": c.get("estimate") is not None and not confounded,
                "estimate": c.get("estimate"), "se": c.get("se"),
                "ci_low": None, "ci_high": None,
                "note": (mp_result["note"] +
                         ("；该处理在参数模型中不可估，此数值同样受混杂影响，不能作为处理效应证据"
                          if confounded else "")),
                "extra": {"treatment": c["treatment"],
                          "n_plates": c.get("n_plates")}})
    return rows


def run_analysis(conn, plate_ids: list[str], k: float = 4.0,
                 explicit_flags: dict[str, bool] | None = None,
                 parent_analysis_id: int | None = None,
                 note: str = "") -> int:
    """对给定板集合创建一个不可变分析快照，返回 analysis_id。

    explicit_flags: {"plate|row|col": True/False}，True=确认坏孔，False=豁免建议。
    """
    plates = storage.list_plates(conn)
    plate_map = {p["plate_id"]: p for p in plates}
    unknown = [p for p in plate_ids if p not in plate_map]
    if unknown:
        raise ValueError(f"板不存在：{unknown}")
    roles = {p: plate_map[p]["role"] for p in plate_ids}
    dims = storage.plate_dims(conn, plate_ids)
    wells = storage.load_wells(conn, plate_ids)
    all_keys = _all_keys(wells)

    # 1) 先用 Huber 全量拟合得到稳健标准化残差与坏孔建议（建议阈值 k 在此冻结）
    probe = stats.fit_huber(wells, dims)
    suggested = set(stats.suggest_bad_wells(probe, k=k))

    # 2) 显式标记覆盖：建议 + 确认坏孔 − 豁免孔；原始记录一律保留
    flags = dict(explicit_flags or {})
    used = set(all_keys)
    for key in suggested:
        if flags.get(key, True):  # 默认采纳建议
            used.discard(key)
    for key, is_bad in flags.items():
        if key not in all_keys:
            raise ValueError(f"标记的孔位不在板集合内：{key}")
        if is_bad:
            used.discard(key)
        else:
            used.add(key)

    # 3) 对照覆盖 → 方法选择（理由随快照冻结）
    coverage = stats.control_coverage(wells, used)
    has_outlier = bool(suggested)
    has_validation = any(r == "validation" for r in roles.values())
    choice = stats.choose_method(coverage, has_outlier, has_validation)

    # 4) 正式拟合
    fitter = FITTERS[choice["method"]]
    fit = fitter(wells, dims, used_keys=used)

    # 5) 中位数抛光交叉核对（始终计算，界面明示其局限）
    mp_result = stats.median_polish_contrasts(wells, dims, used)

    # 6) 独立验证板：单独拟合后与校准板估计对比，结果冻结
    validation_result = None
    val_ids = [p for p in plate_ids if roles[p] == "validation"]
    cal_ids = [p for p in plate_ids if roles[p] == "calibration"]
    if val_ids and cal_ids:
        cal_wells = [w for w in wells if w["plate_id"] in cal_ids]
        cal_dims = {p: dims[p] for p in cal_ids}
        cal_fit = fitter(cal_wells, cal_dims,
                         used_keys={u for u in used if u.split("|")[0] in cal_ids})
        val_fit = fitter(
            [w for w in wells if w["plate_id"] in val_ids],
            {p: dims[p] for p in val_ids},
            used_keys={u for u in used if u.split("|")[0] in val_ids})
        validation_result = stats.validate_contrasts(cal_fit, val_fit)
    elif choice["require_validation"]:
        validation_result = {"passed": False, "n_compared": 0, "n_agreed": 0,
                             "rows": [],
                             "note": "对照分布不足且缺少独立验证板，处理差异未独立验证"}

    payload = {
        "plate_set": plate_ids,
        "roles": roles,
        "method": fit.method,
        "method_choice": {"choice": choice, "coverage": coverage},
        "bad_well_k": k,
        "suggested_bad": sorted(suggested),
        "explicit_flags": flags,
        "used_keys": sorted(used),
        "rank": fit.design.rank,
        "rank_deficient": fit.design.rank < fit.design.n_par,
        "diagnostics": fit.diagnostics,
        "validation_result": validation_result,
        "summary": fit.to_dict(),
        "parent_analysis_id": parent_analysis_id,
        "note": note,
        "well_rows": _well_rows(wells, fit, suggested, used),
        "effect_rows": fit.effects,
        "contrast_rows": _contrast_rows(fit, mp_result),
    }
    # 中位数抛光整体结果挂在 summary 中供前端展示
    payload["summary"]["median_polish_detail"] = mp_result
    return storage.save_analysis(conn, payload)


def rerun_with_flags(conn, parent_id: int, explicit_flags: dict[str, bool],
                     note: str = "") -> int:
    """基于父分析的板集合与阈值创建新版本（只复制配置，不复制结论）。"""
    parent = storage.get_analysis(conn, parent_id)
    if not parent:
        raise ValueError(f"分析 {parent_id} 不存在")
    return run_analysis(
        conn, plate_ids=list(parent["plate_set"]), k=parent["bad_well_k"],
        explicit_flags=explicit_flags, parent_analysis_id=parent_id, note=note)
