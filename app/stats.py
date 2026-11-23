"""统计核心：显式行列/边缘/处理效应模型、可识别性诊断与稳健估计。

模型（板 p、行 i、列 j 的孔位）：
    y_pij = α_p + r_pi + c_pj + e_p·EDGE(i,j) + τ_t(pij) + ε

设计原则见 DESIGN.md：
* 只使用显式参数模型与中位数抛光交叉核对，不做二维平滑（防止抹掉真实处理效应）；
* 先 SVD 判秩，用零空间向量给出"无法分开的具体项"；
* 不可估的对比不给数值，只给混杂说明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
from scipy import stats as sp_stats

# ---- 效应族 -------------------------------------------------------------
FAMILY_PLATE = "plate"
FAMILY_ROW = "row"
FAMILY_COL = "col"
FAMILY_EDGE = "edge"
FAMILY_TREATMENT = "treatment"

FAMILY_LABELS = {
    FAMILY_PLATE: "板间截距",
    FAMILY_ROW: "行效应",
    FAMILY_COL: "列效应",
    FAMILY_EDGE: "边缘效应",
    FAMILY_TREATMENT: "处理效应",
}

CONTROL_NAME = "CTRL"
EST_TOL = 1e-8
HUBER_DELTA = 1.345


# ---- 设计矩阵 -----------------------------------------------------------
@dataclass
class ColumnMeta:
    """设计矩阵一列的语义。"""

    family: str
    plate: str | None  # treatment 列为 None（跨板共同效应）
    level: str  # 水平名称（板号/行列号/edge/处理名）

    def label(self) -> str:
        fam = FAMILY_LABELS[self.family]
        if self.family == FAMILY_TREATMENT:
            return f"{fam}[{self.level}]"
        if self.family == FAMILY_PLATE:
            return f"{fam}[{self.plate}]"
        return f"{fam}[{self.plate}/{self.level}]"


@dataclass
class Design:
    X: np.ndarray
    y: np.ndarray
    well_keys: list[str]  # 每行对应的 well key: "plate|row|col"
    columns: list[ColumnMeta]
    col_index: dict[str, int]  # ColumnMeta.label() -> 列号
    treatment_ref: str
    treatments: list[str]
    rank: int
    V0: np.ndarray  # 零空间基 (p × k)
    singular_values: list[float]

    @property
    def n_obs(self) -> int:
        return self.X.shape[0]

    @property
    def n_par(self) -> int:
        return self.X.shape[1]


def _row_name(i: int) -> str:
    # 8 行以内用 A..H，超出用数字
    return chr(ord("A") + i) if i < 26 else f"R{i + 1}"


def _edge_flag(row_idx: int, col_idx: int, n_rows: int, n_cols: int) -> int:
    return int(row_idx in (0, n_rows - 1) or col_idx in (0, n_cols - 1))


def build_design(records: list[dict], plate_dims: dict[str, tuple[int, int]],
                 treatment_ref: str = CONTROL_NAME) -> Design:
    """根据孔位记录构造设计矩阵。

    records 每项需含：plate_id, row_idx, col_idx, treatment, response；
    edge 缺省时由 plate_dims 计算（周边=1）。
    参考水平：每板首行/首列；处理参考为 CTRL（不存在则取排序后首个处理）。
    """
    plate_ids = sorted(plate_dims.keys())
    treatments = sorted({r["treatment"] for r in records})
    if treatment_ref not in treatments:
        treatment_ref = treatments[0]
    treat_nonref = [t for t in treatments if t != treatment_ref]

    columns: list[ColumnMeta] = []
    col_index: dict[str, int] = {}

    def add_col(meta: ColumnMeta) -> int:
        idx = len(columns)
        columns.append(meta)
        col_index[meta.label()] = idx
        return idx

    # 列顺序：板截距 → 行 → 列 → 边缘 → 处理
    for p in plate_ids:
        add_col(ColumnMeta(FAMILY_PLATE, p, "intercept"))
        n_rows, n_cols = plate_dims[p]
        for i in range(1, n_rows):
            add_col(ColumnMeta(FAMILY_ROW, p, _row_name(i)))
        for j in range(1, n_cols):
            add_col(ColumnMeta(FAMILY_COL, p, f"C{j + 1}"))
        add_col(ColumnMeta(FAMILY_EDGE, p, "edge"))
    for t in treat_nonref:
        add_col(ColumnMeta(FAMILY_TREATMENT, None, t))

    X = np.zeros((len(records), len(columns)), dtype=float)
    y = np.zeros(len(records), dtype=float)
    keys: list[str] = []
    for k, rec in enumerate(records):
        p = rec["plate_id"]
        i, j = rec["row_idx"], rec["col_idx"]
        n_rows, n_cols = plate_dims[p]
        edge = rec.get("edge", _edge_flag(i, j, n_rows, n_cols))
        X[k, col_index[ColumnMeta(FAMILY_PLATE, p, "intercept").label()]] = 1.0
        if i >= 1:
            X[k, col_index[ColumnMeta(FAMILY_ROW, p, _row_name(i)).label()]] = 1.0
        if j >= 1:
            X[k, col_index[ColumnMeta(FAMILY_COL, p, f"C{j + 1}").label()]] = 1.0
        if edge:
            X[k, col_index[ColumnMeta(FAMILY_EDGE, p, "edge").label()]] = 1.0
        t = rec["treatment"]
        if t != treatment_ref:
            X[k, col_index[ColumnMeta(FAMILY_TREATMENT, None, t).label()]] = 1.0
        y[k] = rec["response"]
        keys.append(f"{p}|{i}|{j}")

    # SVD 判秩
    _, s, Vt = np.linalg.svd(X, full_matrices=False)
    tol = max(X.shape) * np.finfo(float).eps * (s[0] if len(s) else 1.0)
    rank = int(np.sum(s > tol))
    V0 = Vt[rank:].T  # p × k
    return Design(X=X, y=y, well_keys=keys, columns=columns, col_index=col_index,
                  treatment_ref=treatment_ref, treatments=treatments, rank=rank,
                  V0=V0, singular_values=[float(v) for v in s])


# ---- 可估性与混杂诊断 ----------------------------------------------------
def is_estimable(contrast: np.ndarray, V0: np.ndarray, tol: float = EST_TOL) -> bool:
    """c 可估 ⇔ c 与零空间正交（P0 c ≈ 0）。"""
    if V0.shape[1] == 0:
        return True
    proj = V0 @ (V0.T @ contrast)
    return float(np.linalg.norm(proj)) <= tol * max(float(np.linalg.norm(contrast)), 1.0)


def null_diagnostics(design: Design) -> list[dict]:
    """把每个零空间向量翻译成人话的混杂诊断。"""
    out: list[dict] = []
    for k in range(design.V0.shape[1]):
        v = design.V0[:, k]
        active = np.where(np.abs(v) > EST_TOL)[0]
        if len(active) == 0:
            continue
        families = sorted({design.columns[a].family for a in active})
        involved = [
            {"column": design.columns[a].label(),
             "family": design.columns[a].family,
             "weight": float(v[a])}
            for a in active
        ]
        fam_set = set(families)
        # 零向量常同时包含板截距列（参考水平补偿），按实质性效应族子集判定
        substantive = fam_set - {FAMILY_PLATE}
        if substantive == {FAMILY_ROW, FAMILY_TREATMENT}:
            message = ("整行同处理：处理差异与行效应完全混杂，"
                       "现有设计无法把二者分开（需要在同行内安排不同处理）")
            kind = "row_treatment_confounded"
        elif substantive == {FAMILY_COL, FAMILY_TREATMENT}:
            message = ("处理按整列排布：处理差异与列效应完全混杂，"
                       "现有设计无法把二者分开")
            kind = "col_treatment_confounded"
        elif substantive == {FAMILY_EDGE, FAMILY_TREATMENT}:
            message = ("边缘位置缺少对照且相关处理只出现在边缘：边缘效应与处理差异完全混杂，"
                       "需要在边缘布置对照或让该处理同时出现在内部孔位")
            kind = "edge_treatment_confounded"
        elif substantive <= {FAMILY_ROW}:
            message = "行效应内部存在线性相关，部分行效应不可识别"
            kind = "row_aliased"
        elif substantive <= {FAMILY_COL}:
            message = "列效应内部存在线性相关，部分列效应不可识别"
            kind = "col_aliased"
        else:
            names = "、".join(design.columns[a].label() for a in active)
            message = f"以下参数线性相关、不可同时识别：{names}"
            kind = "generic_aliasing"
        out.append({"kind": kind, "families": families,
                    "message": message, "involved": involved})
    return out


def contrast_confound_note(design: Design, contrast: np.ndarray) -> str | None:
    """给定对比 c，找出把它拖入不可估的零空间方向并汇总涉及的效应族。"""
    if design.V0.shape[1] == 0:
        return None
    families: set[str] = set()
    hit = False
    for k in range(design.V0.shape[1]):
        v = design.V0[:, k]
        if np.linalg.norm(v * contrast) > EST_TOL:
            hit = True
            active = np.where(np.abs(v) > EST_TOL)[0]
            families.update(design.columns[a].family for a in active)
    if not hit:
        return None
    labels = "、".join(FAMILY_LABELS[f] for f in sorted(families))
    return f"该对比与{labels}完全混杂，当前板上不可估"


# ---- 拟合结果 -----------------------------------------------------------
@dataclass
class FitResult:
    method: str
    design: Design
    beta: np.ndarray
    se: np.ndarray
    fitted: np.ndarray
    residuals: np.ndarray
    rstd: np.ndarray  # 标准化残差（好孔）；排除孔为 NaN
    sigma: float
    dof: int
    used_mask: np.ndarray  # 参与拟合的好孔
    diagnostics: list[dict]
    effects: list[dict]
    contrasts: list[dict]
    pairwise: list[dict]
    median_polish: dict | None = None

    def to_dict(self) -> dict:
        d = self.design
        return {
            "method": self.method,
            "n_obs": d.n_obs,
            "n_par": d.n_par,
            "rank": d.rank,
            "rank_deficient": d.rank < d.n_par,
            "dof": self.dof,
            "sigma": self.sigma,
            "diagnostics": self.diagnostics,
            "effects": self.effects,
            "contrasts": self.contrasts,
            "pairwise": self.pairwise,
            "median_polish": self.median_polish,
            "singular_values": d.singular_values,
            "treatment_ref": d.treatment_ref,
        }


def _family_effects(design: Design, beta: np.ndarray, cov: np.ndarray,
                    V0: np.ndarray, dof: int) -> list[dict]:
    """逐列效应：单位列向量本身是可估函数时才报数值。"""
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    out = []
    t_crit = float(sp_stats.t.ppf(0.975, max(dof, 1)))
    for j, meta in enumerate(design.columns):
        c = np.zeros(design.n_par)
        c[j] = 1.0
        estimable = is_estimable(c, V0)
        item = {"column": meta.label(), "family": meta.family,
                "plate": meta.plate, "level": meta.level,
                "estimable": estimable}
        if estimable:
            item.update(estimate=float(beta[j]), se=float(se[j]),
                        ci_low=float(beta[j] - t_crit * se[j]),
                        ci_high=float(beta[j] + t_crit * se[j]))
        else:
            item.update(estimate=None, se=None, ci_low=None, ci_high=None,
                        note=contrast_confound_note(design, c))
        out.append(item)
    return out


def _treatment_contrasts(design: Design, beta: np.ndarray, cov: np.ndarray,
                         dof: int) -> tuple[list[dict], list[dict]]:
    t_crit = float(sp_stats.t.ppf(0.975, max(dof, 1)))
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    contrasts: list[dict] = []
    idx_of = {}
    for t in design.treatments:
        if t == design.treatment_ref:
            continue
        idx = design.col_index[ColumnMeta(FAMILY_TREATMENT, None, t).label()]
        idx_of[t] = idx
        c = np.zeros(design.n_par)
        c[idx] = 1.0
        item = {"treatment": t, "vs": design.treatment_ref,
                "estimable": is_estimable(c, design.V0)}
        if item["estimable"]:
            est, s = float(beta[idx]), float(se[idx])
            item.update(estimate=est, se=s,
                        ci_low=est - t_crit * s, ci_high=est + t_crit * s)
        else:
            item.update(estimate=None, se=None, ci_low=None, ci_high=None,
                        note=contrast_confound_note(design, c))
        contrasts.append(item)

    pairwise: list[dict] = []
    names = list(idx_of.keys())
    for a_i in range(len(names)):
        for b_i in range(a_i + 1, len(names)):
            a, b = names[a_i], names[b_i]
            c = np.zeros(design.n_par)
            c[idx_of[a]] = 1.0
            c[idx_of[b]] = -1.0
            item = {"a": a, "b": b, "estimable": is_estimable(c, design.V0)}
            if item["estimable"]:
                est = float(beta[idx_of[a]] - beta[idx_of[b]])
                # 完整协方差：Var(a-b) = Var(a) + Var(b) − 2Cov(a,b)
                s = float(math.sqrt(max(c @ cov @ c, 0.0)))
                item.update(estimate=est, se=s,
                            ci_low=est - t_crit * s, ci_high=est + t_crit * s)
            else:
                item.update(estimate=None, se=None, ci_low=None, ci_high=None,
                            note=contrast_confound_note(design, c))
            pairwise.append(item)
    return contrasts, pairwise


def _mad(x: np.ndarray) -> float:
    med = np.median(x)
    return float(np.median(np.abs(x - med)))


def fit_ols(records: list[dict], plate_dims: dict[str, tuple[int, int]],
            used_keys: set[str] | None = None) -> FitResult:
    """OLS 拟合（好孔子矩阵）；used_keys 之外的孔保留位置但不参与估计。"""
    design = build_design(records, plate_dims)
    n = design.n_obs
    used_mask = np.array(
        [used_keys is None or k in used_keys for k in design.well_keys])
    Xg, yg = design.X[used_mask], design.y[used_mask]

    beta, *_ = np.linalg.lstsq(Xg, yg, rcond=None)
    dof = max(int(used_mask.sum()) - design.rank, 1)
    resid_g = yg - Xg @ beta
    s2 = float(resid_g @ resid_g) / dof
    pinv = np.linalg.pinv(Xg.T @ Xg)
    cov = s2 * pinv
    se_g = np.sqrt(np.maximum(np.diag(cov), 0.0))

    # 全量孔位的拟合/残差（被排除孔也给拟合值，残差仅作参考）
    fitted = design.X @ beta
    residuals = design.y - fitted
    hat_g = np.einsum("ij,jk,ik->i", Xg, pinv, Xg)
    rstd = np.full(n, np.nan)
    rstd_g = resid_g / np.sqrt(max(s2, 1e-12) * np.maximum(1.0 - hat_g, 1e-6))
    rstd[used_mask] = rstd_g

    effects = _family_effects(design, beta, cov, design.V0, dof)
    contrasts, pairwise = _treatment_contrasts(design, beta, cov, dof)
    return FitResult(
        method="ols", design=design, beta=beta, se=se_g, fitted=fitted,
        residuals=residuals, rstd=rstd, sigma=math.sqrt(s2), dof=dof,
        used_mask=used_mask, diagnostics=null_diagnostics(design),
        effects=effects, contrasts=contrasts, pairwise=pairwise)


def fit_huber(records: list[dict], plate_dims: dict[str, tuple[int, int]],
              used_keys: set[str] | None = None,
              max_iter: int = 100, tol: float = 1e-7) -> FitResult:
    """同参数模型的 Huber IRLS 稳健拟合。"""
    design = build_design(records, plate_dims)
    n = design.n_obs
    used_mask = np.array(
        [used_keys is None or k in used_keys for k in design.well_keys])
    Xg, yg = design.X[used_mask], design.y[used_mask]

    beta0, *_ = np.linalg.lstsq(Xg, yg, rcond=None)
    # 尺度只从初始 OLS 残差的 MAD 估计一次并冻结：
    # 迭代中反复重估会被 Huber 权重系统性压低，低估 σ 并放大标准化残差（误报坏孔）。
    scale = max(1.4826 * _mad(yg - Xg @ beta0), 1e-12)
    beta = beta0
    weights = np.ones(len(yg))
    for _ in range(max_iter):
        r = yg - Xg @ beta
        u = r / (HUBER_DELTA * scale)
        new_w = np.where(np.abs(u) <= 1.0, 1.0, 1.0 / np.maximum(np.abs(u), 1e-12))
        sw = np.sqrt(new_w)
        beta_new, *_ = np.linalg.lstsq(Xg * sw[:, None], yg * sw, rcond=None)
        if np.max(np.abs(beta_new - beta)) < tol:
            beta, weights = beta_new, new_w
            break
        beta, weights = beta_new, new_w

    dof = max(int(used_mask.sum()) - design.rank, 1)
    resid_g = yg - Xg @ beta
    sigma = scale
    XtWX = Xg.T @ (Xg * weights[:, None])
    pinv_w = np.linalg.pinv(XtWX)
    # Huber proposal 2 协方差修正：κ = mean(ψ²) / mean(ψ′)²
    u = resid_g / (HUBER_DELTA * sigma)
    psi = np.where(np.abs(u) <= 1.0, u, np.sign(u))
    psi_prime = (np.abs(u) <= 1.0).astype(float)
    kappa = float(np.mean(psi ** 2) / max(np.mean(psi_prime) ** 2, 1e-12))
    cov = kappa * sigma ** 2 * pinv_w
    se_g = np.sqrt(np.maximum(np.diag(cov), 0.0))

    fitted = design.X @ beta
    residuals = design.y - fitted
    rstd = np.full(n, np.nan)
    hat_g = np.einsum("ij,jk,ik->i", Xg, pinv_w, Xg)
    rstd_g = resid_g / np.maximum(sigma * np.sqrt(np.maximum(1.0 - hat_g, 1e-6)), 1e-12)
    rstd[used_mask] = rstd_g

    effects = _family_effects(design, beta, cov, design.V0, dof)
    contrasts, pairwise = _treatment_contrasts(design, beta, cov, dof)
    return FitResult(
        method="huber", design=design, beta=beta, se=se_g, fitted=fitted,
        residuals=residuals, rstd=rstd, sigma=float(sigma), dof=dof,
        used_mask=used_mask, diagnostics=null_diagnostics(design),
        effects=effects, contrasts=contrasts, pairwise=pairwise)


# ---- 中位数抛光（非参数交叉核对）-----------------------------------------
def _nan_median(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    return float(np.median(x)) if len(x) else 0.0


def median_polish(mat: np.ndarray, max_iter: int = 200,
                  tol: float = 1e-8) -> dict:
    """Tukey 中位数抛光，支持 NaN 缺孔。返回 overall/row/col/remainder。"""
    z = mat.astype(float).copy()
    m, n = z.shape
    row_eff = np.zeros(m)
    col_eff = np.zeros(n)
    overall = 0.0
    scale = max(np.nanstd(z), 1e-8)
    for _ in range(max_iter):
        row_med = np.array([_nan_median(z[i, :]) for i in range(m)])
        z -= row_med[:, None]
        row_eff += row_med
        col_med = np.array([_nan_median(z[:, j]) for j in range(n)])
        z -= col_med[None, :]
        col_eff += col_med
        g = _nan_median(row_eff)
        row_eff -= g
        overall += g
        if max(np.max(np.abs(row_med)), np.max(np.abs(col_med))) < tol * scale:
            break
    return {"overall": float(overall), "row": row_eff.tolist(),
            "col": col_eff.tolist(), "remainder": z}


def median_polish_contrasts(records: list[dict],
                            plate_dims: dict[str, tuple[int, int]],
                            used_keys: set[str] | None) -> dict:
    """逐板中位数抛光后，用残差矩阵内的处理均值估计处理位移（相对 CTRL）。

    注意：该方法不单独估计边缘效应，边缘信号被行列效应与残差吸收；
    估计结果继承设计本身的混杂，仅作交叉核对。
    """
    by_plate: dict[str, dict] = {}
    for p, (n_rows, n_cols) in plate_dims.items():
        mat = np.full((n_rows, n_cols), np.nan)
        treat_map: dict[tuple[int, int], str] = {}
        for rec in records:
            if rec["plate_id"] != p:
                continue
            key = f"{p}|{rec['row_idx']}|{rec['col_idx']}"
            if used_keys is not None and key not in used_keys:
                continue
            mat[rec["row_idx"], rec["col_idx"]] = rec["response"]
            treat_map[(rec["row_idx"], rec["col_idx"])] = rec["treatment"]
        mp = median_polish(mat)
        rem = np.array(mp["remainder"])
        per_treat: dict[str, list[float]] = {}
        for (i, j), t in treat_map.items():
            if not np.isnan(rem[i, j]):
                per_treat.setdefault(t, []).append(rem[i, j])
        means = {t: float(np.mean(v)) for t, v in per_treat.items()}
        by_plate[p] = {"polish": mp, "means": means}

    treatments = sorted({rec["treatment"] for rec in records})
    ref = CONTROL_NAME if CONTROL_NAME in treatments else treatments[0]
    plates = sorted(plate_dims.keys())
    out = {"method": "median_polish", "reference": ref,
           "note": "中位数抛光不分离边缘效应，仅作参数模型的交叉核对",
           "contrasts": []}
    for t in treatments:
        if t == ref:
            continue
        per_plate_d, per_plate_se = [], []
        for p in plates:
            means = by_plate[p]["means"]
            if t in means and ref in means:
                per_plate_d.append(means[t] - means[ref])
        if not per_plate_d:
            out["contrasts"].append({"treatment": t, "estimate": None,
                                     "se": None, "note": "该板无法计算对比"})
            continue
        est = float(np.mean(per_plate_d))
        se = float(np.std(per_plate_d, ddof=1) / math.sqrt(len(per_plate_d))) \
            if len(per_plate_d) > 1 else None
        out["contrasts"].append(
            {"treatment": t, "estimate": est, "se": se,
             "n_plates": len(per_plate_d)})
    return out


# ---- 对照覆盖与方法选择 --------------------------------------------------
def control_coverage(records: list[dict], used_keys: set[str]) -> dict:
    rows, cols, edge_vals = set(), set(), set()
    n_ctrl = 0
    for rec in records:
        key = f"{rec['plate_id']}|{rec['row_idx']}|{rec['col_idx']}"
        if key not in used_keys or rec["treatment"] != CONTROL_NAME:
            continue
        n_ctrl += 1
        rows.add((rec["plate_id"], rec["row_idx"]))
        cols.add((rec["plate_id"], rec["col_idx"]))
        edge_vals.add(rec.get("edge", 0))
    # 跨板汇总：每板至少出现 2 行 2 列，且边缘/内部都出现过对照
    per_plate_rows: dict[str, set] = {}
    per_plate_cols: dict[str, set] = {}
    for p, i in rows:
        per_plate_rows.setdefault(p, set()).add(i)
    for p, j in cols:
        per_plate_cols.setdefault(p, set()).add(j)
    row_ok = all(len(v) >= 2 for v in per_plate_rows.values()) and bool(per_plate_rows)
    col_ok = all(len(v) >= 2 for v in per_plate_cols.values()) and bool(per_plate_cols)
    edge_ok = edge_vals == {0, 1}
    return {
        "n_control_wells": n_ctrl,
        "n_rows_with_control": len({p for p, _ in rows}),
        "n_cols_with_control": len({p for p, _ in cols}),
        "row_ok": row_ok, "col_ok": col_ok,
        "edge_ok": edge_ok,
        "sufficient": bool(row_ok and col_ok and edge_ok),
    }


def suggest_bad_wells(fit: FitResult, k: float = 4.0) -> list[str]:
    """以稳健标准化残差 |r_std| > k 给出坏孔建议（不删除任何记录）。"""
    out = []
    for idx, key in enumerate(fit.design.well_keys):
        if not fit.used_mask[idx]:
            continue
        if abs(fit.rstd[idx]) > k:
            out.append(key)
    return out


def choose_method(coverage: dict, has_outlier: bool,
                  has_validation_plate: bool) -> dict:
    """方法选择规则（结果随分析快照冻结，详见 DESIGN.md 2.3）。"""
    reasons = []
    if coverage["sufficient"]:
        if has_outlier:
            method = "huber"
            reasons.append("对照在行列与边缘覆盖充分，但存在离群孔，采用 Huber 稳健参数模型")
        else:
            method = "ols"
            reasons.append("对照在行列与边缘覆盖充分且无显著离群，采用 OLS 参数模型")
        require_validation = False
    else:
        method = "huber"
        require_validation = True
        missing = []
        if not coverage["row_ok"]:
            missing.append("对照未覆盖至少 2 行")
        if not coverage["col_ok"]:
            missing.append("对照未覆盖至少 2 列")
        if not coverage["edge_ok"]:
            missing.append("对照未同时出现在边缘与内部孔位")
        reasons.append("对照分布不足（" + "；".join(missing) +
                       "）：采用稳健空间校正，处理差异必须由独立验证板确认")
    if not has_validation_plate:
        reasons.append("缺少独立验证板：处理差异结论标注为未独立验证")
    return {"method": method, "reasons": reasons,
            "require_validation": require_validation}


# ---- 独立验证板 ----------------------------------------------------------
def validate_contrasts(calib: FitResult, validation: FitResult) -> dict:
    """逐处理比较校准板与验证板的处理效应，冻结验证结论。

    同时检验 m 个处理对比，阈值用 Bonferroni 校正的 t 临界值
    （m=1 时即常规 95% 双侧区间），避免多重比较下的边缘假阳性。
    """
    cal = {c["treatment"]: c for c in calib.contrasts}
    val = {c["treatment"]: c for c in validation.contrasts}
    m = len(cal)
    alpha = 0.05 / max(m, 1)
    dof = max(min(calib.dof, validation.dof), 1)
    t_crit = float(sp_stats.t.ppf(1.0 - alpha / 2.0, dof))
    rows = []
    n_agree = n_compare = 0
    for t, c1 in cal.items():
        c2 = val.get(t)
        row = {"treatment": t}
        if not c1["estimable"] or c2 is None or not c2["estimable"]:
            row.update(status="not_estimable",
                       note="至少一侧不可估，无法独立验证")
        else:
            d = c2["estimate"] - c1["estimate"]
            se_d = math.sqrt(c1["se"] ** 2 + c2["se"] ** 2)
            band = t_crit * se_d
            agree = abs(d) <= band
            n_compare += 1
            n_agree += int(agree)
            row.update(status="agree" if agree else "disagree",
                       calib_estimate=c1["estimate"],
                       validation_estimate=c2["estimate"],
                       difference=d, se_difference=se_d,
                       ci_low=d - band, ci_high=d + band,
                       multiplicity_adjusted=True, m_comparisons=m)
        rows.append(row)
    passed = n_compare > 0 and n_agree == n_compare
    return {"passed": passed, "n_compared": n_compare,
            "n_agreed": n_agree, "t_critical": t_crit, "rows": rows}
