"""合成实验板数据生成（全部虚构，固定随机种子）。

三场景见 DESIGN.md 第 4 节：
A 随机布局（2 校准 + 1 验证）、B 整行同处理（完全混杂）、C 边缘对照缺失。
"""
from __future__ import annotations

import random

N_ROWS, N_COLS = 8, 12
CONTROL = "CTRL"
TREATMENTS = ["T1", "T2", "T3", "T4", "T5"]
TREATMENT_EFFECTS = {"CTRL": 0.0, "T1": -3.0, "T2": 5.0, "T3": 9.0,
                     "T4": -7.0, "T5": 12.0}
# 真值：自上而下的行漂移（上边缘偏高）、小幅列漂移、边缘效应、噪声
ROW_EFFECTS = [6.0, 4.0, 2.5, 1.0, -0.5, -2.0, -3.5, -5.0]
COL_EFFECTS = [(-1.5 + 3.0 * j / (N_COLS - 1)) for j in range(N_COLS)]
EDGE_EFFECT = 8.0
INTERCEPT = 100.0
NOISE_SD = 1.5
OUTLIER_STEP = 15.0


def _edge(i: int, j: int) -> int:
    return int(i in (0, N_ROWS - 1) or j in (0, N_COLS - 1))


def _make_well(plate_id: str, i: int, j: int, treatment: str, rng: random.Random,
               outlier: bool = False) -> dict:
    err = rng.gauss(0.0, NOISE_SD)
    if outlier:
        err += OUTLIER_STEP * rng.choice([-1, 1])
    response = (INTERCEPT + ROW_EFFECTS[i] + COL_EFFECTS[j]
                + EDGE_EFFECT * _edge(i, j)
                + TREATMENT_EFFECTS[treatment] + err)
    return {
        "plate_id": plate_id,
        "row_idx": i,
        "col_idx": j,
        "row_name": chr(ord("A") + i),
        "col_name": str(j + 1),
        "edge": _edge(i, j),
        "treatment": treatment,
        "replicate_group": f"{plate_id}-{treatment}",
        "response": round(response, 4),
        "is_outlier_truth": outlier,
    }


def _positions() -> list[tuple[int, int]]:
    return [(i, j) for i in range(N_ROWS) for j in range(N_COLS)]


def _assign_random(rng: random.Random) -> dict[tuple[int, int], str]:
    """随机平衡布局：96 孔按 6 处理（含 CTRL）均分后打乱。"""
    bag = []
    names = [CONTROL] + TREATMENTS
    per, rem = divmod(N_ROWS * N_COLS, len(names))
    for k, name in enumerate(names):
        bag += [name] * (per + (1 if k < rem else 0))
    rng.shuffle(bag)
    return dict(zip(_positions(), bag))


def _assign_row_confounded(rng: random.Random) -> dict[tuple[int, int], str]:
    """整行同处理：每行一个处理，行效应与处理效应完全混杂。"""
    names = [CONTROL] + TREATMENTS  # 6 个处理 / 8 行，部分处理重复两行
    row_names = names + rng.sample(names, N_ROWS - len(names))
    rng.shuffle(row_names)
    return {(i, j): row_names[i] for i in range(N_ROWS) for j in range(N_COLS)}


def _assign_edge_missing(rng: random.Random) -> dict[tuple[int, int], str]:
    """对照只在内部 6×8；T5 占据全部周边。

    此时边缘指示变量与 T5 指示变量在每块板上完全相同 → 两列线性相关、
    边缘效应与 T5 处理效应完全混杂（这是产生不可识别提示的必要条件）。
    """
    layout: dict[tuple[int, int], str] = {}
    interior = [(i, j) for i, j in _positions() if _edge(i, j) == 0]
    perimeter = [(i, j) for i, j in _positions() if _edge(i, j) == 1]
    # 周边 48 孔全部为 T5，且 T5 不出现在内部
    for pos in perimeter:
        layout[pos] = "T5"
    # 内部 48 孔：CTRL 占一半，T1..T4 均分其余（内部无 T5）
    inner_names = [CONTROL, CONTROL, "T1", "T2", "T3", "T4"]
    per = len(interior) // len(inner_names)
    bag = [name for name in inner_names for _ in range(per)]
    rng.shuffle(bag)
    for pos, name in zip(interior, bag):
        layout[pos] = name
    return layout


def _build_plate(plate_id: str, role: str, scenario: str,
                 layout: dict[tuple[int, int], str], rng: random.Random,
                 outlier_positions: list[tuple[int, int]]) -> dict:
    wells = []
    out_set = set(outlier_positions)
    for (i, j), treatment in sorted(layout.items()):
        wells.append(_make_well(plate_id, i, j, treatment, rng,
                                outlier=(i, j) in out_set))
    return {
        "plate_id": plate_id,
        "name": plate_id,
        "role": role,  # calibration / validation，导入即固定
        "scenario": scenario,
        "n_rows": N_ROWS,
        "n_cols": N_COLS,
        "wells": wells,
    }


def generate_scenario_a(seed: int = 20261123) -> dict:
    rng = random.Random(seed)
    plates = []
    # 两板校准 + 一板独立验证；每板 3 个离群坏孔
    for k, (pid, role) in enumerate([
            ("A-CAL-1", "calibration"), ("A-CAL-2", "calibration"),
            ("A-VAL-1", "validation")]):
        layout = _assign_random(rng)
        outliers = rng.sample(_positions(), 3)
        plates.append(_build_plate(pid, role, "A_random", layout, rng, outliers))
    return {"scenario": "A_random",
            "description": "随机平衡布局：对照遍布行列与边缘，2 校准板 + 1 独立验证板",
            "plates": plates, "ground_truth": _ground_truth()}


def generate_scenario_b(seed: int = 20261124) -> dict:
    rng = random.Random(seed)
    plates = []
    for k, (pid, role) in enumerate([
            ("B-CAL-1", "calibration"), ("B-CAL-2", "calibration"),
            ("B-VAL-1", "validation")]):
        layout = _assign_row_confounded(rng)
        outliers = rng.sample(_positions(), 3)
        plates.append(_build_plate(pid, role, "B_row_confounded", layout, rng, outliers))
    return {"scenario": "B_row_confounded",
            "description": "整行同处理：处理与行效应完全混杂，所有处理对比应判不可估",
            "plates": plates, "ground_truth": _ground_truth()}


def generate_scenario_c(seed: int = 20261125) -> dict:
    rng = random.Random(seed)
    plates = []
    # 只有校准板、无验证板：方法选择必须提示"未独立验证"
    for pid in ("C-CAL-1", "C-CAL-2"):
        layout = _assign_edge_missing(rng)
        outliers = rng.sample(
            [(i, j) for (i, j) in _positions() if _edge(i, j) == 0], 2)
        plates.append(_build_plate(pid, "calibration", "C_edge_missing",
                                   layout, rng, outliers))
    return {"scenario": "C_edge_missing",
            "description": "边缘对照缺失：CTRL 仅在内部，T5 占据全部周边，边缘效应与 T5 完全混杂；无验证板",
            "plates": plates, "ground_truth": _ground_truth()}


def _ground_truth() -> dict:
    return {
        "intercept": INTERCEPT,
        "row_effects": ROW_EFFECTS,
        "col_effects": COL_EFFECTS,
        "edge_effect": EDGE_EFFECT,
        "treatment_effects": TREATMENT_EFFECTS,
        "noise_sd": NOISE_SD,
        "outlier_step": OUTLIER_STEP,
    }


def generate_all() -> list[dict]:
    return [generate_scenario_a(), generate_scenario_b(), generate_scenario_c()]
