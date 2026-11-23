"""SQLite 持久层：原始数据只追加，分析结果为不可变快照。

不变量（DESIGN.md 第 3 节）：
* wells 表只有 INSERT，没有 UPDATE/DELETE —— 坏孔标记不写回原记录；
* analyses 及其子表一旦写入永不修改；追加新板只新增 plates/wells 与新分析；
* 板角色 role 在导入时固定，之后没有任何接口可以修改。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("PLATE_DB", "data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plates (
    plate_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('calibration','validation')),
    scenario     TEXT NOT NULL,
    n_rows       INTEGER NOT NULL,
    n_cols       INTEGER NOT NULL,
    import_seq   INTEGER NOT NULL,
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wells (
    plate_id         TEXT NOT NULL,
    row_idx          INTEGER NOT NULL,
    col_idx          INTEGER NOT NULL,
    row_name         TEXT NOT NULL,
    col_name         TEXT NOT NULL,
    edge             INTEGER NOT NULL,
    treatment        TEXT NOT NULL,
    replicate_group  TEXT NOT NULL,
    response         REAL NOT NULL,
    is_outlier_truth INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plate_id, row_idx, col_idx),
    FOREIGN KEY (plate_id) REFERENCES plates(plate_id)
);

CREATE TABLE IF NOT EXISTS analyses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    plate_set           TEXT NOT NULL,          -- JSON 板ID列表（创建时冻结）
    roles               TEXT NOT NULL,          -- JSON 板角色映射
    method              TEXT NOT NULL,
    method_choice       TEXT NOT NULL,          -- JSON 选择理由与覆盖度
    bad_well_k          REAL NOT NULL,          -- 建议阈值（创建时冻结）
    suggested_bad       TEXT NOT NULL,          -- JSON 建议坏孔 key 列表
    explicit_flags      TEXT NOT NULL,          -- JSON 显式标记覆盖
    used_keys           TEXT NOT NULL,          -- JSON 实际参与拟合的 key
    rank                INTEGER NOT NULL,
    rank_deficient      INTEGER NOT NULL,
    diagnostics         TEXT NOT NULL,          -- JSON 零空间诊断
    validation_result   TEXT,                   -- JSON 独立验证结果（可空）
    summary             TEXT NOT NULL,          -- JSON 完整 fit 结果
    parent_analysis_id  INTEGER,
    note                TEXT
);

CREATE TABLE IF NOT EXISTS analysis_wells (
    analysis_id      INTEGER NOT NULL,
    plate_id         TEXT NOT NULL,
    row_idx          INTEGER NOT NULL,
    col_idx          INTEGER NOT NULL,
    treatment        TEXT NOT NULL,
    edge             INTEGER NOT NULL,
    replicate_group  TEXT NOT NULL DEFAULT '',
    response         REAL NOT NULL,
    fitted        REAL NOT NULL,
    residual      REAL NOT NULL,
    rstd          REAL,
    used_in_fit   INTEGER NOT NULL,
    suggested_bad INTEGER NOT NULL,
    flagged_bad   INTEGER NOT NULL,
    PRIMARY KEY (analysis_id, plate_id, row_idx, col_idx)
);

CREATE TABLE IF NOT EXISTS analysis_effects (
    analysis_id INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    family      TEXT NOT NULL,
    plate_id    TEXT,
    level       TEXT NOT NULL,
    estimable   INTEGER NOT NULL,
    estimate    REAL,
    se          REAL,
    ci_low      REAL,
    ci_high     REAL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS analysis_contrasts (
    analysis_id INTEGER NOT NULL,
    kind        TEXT NOT NULL,       -- treatment / pairwise / validation / median_polish
    label       TEXT NOT NULL,
    estimable   INTEGER NOT NULL,
    estimate    REAL,
    se          REAL,
    ci_low      REAL,
    ci_high     REAL,
    note        TEXT,
    extra       TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | None = None, check_same_thread: bool = True) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


# ---- 原始数据导入（只追加）----------------------------------------------
def import_plate(conn: sqlite3.Connection, plate: dict) -> None:
    """导入一块板；plate_id 已存在则拒绝，角色在此固定。"""
    existing = conn.execute(
        "SELECT 1 FROM plates WHERE plate_id = ?", (plate["plate_id"],)).fetchone()
    if existing:
        raise ValueError(f"板 {plate['plate_id']} 已存在，禁止重复导入")
    seq = conn.execute("SELECT COALESCE(MAX(import_seq), 0) + 1 FROM plates").fetchone()[0]
    conn.execute(
        "INSERT INTO plates VALUES (?,?,?,?,?,?,?,?)",
        (plate["plate_id"], plate["name"], plate["role"], plate["scenario"],
         plate["n_rows"], plate["n_cols"], seq, _now()))
    conn.executemany(
        """INSERT INTO wells (plate_id,row_idx,col_idx,row_name,col_name,edge,
                              treatment,replicate_group,response,is_outlier_truth)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(w["plate_id"], w["row_idx"], w["col_idx"], w["row_name"], w["col_name"],
          w["edge"], w["treatment"], w["replicate_group"], w["response"],
          int(w.get("is_outlier_truth", False))) for w in plate["wells"]])
    conn.commit()


def list_plates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM plates ORDER BY import_seq").fetchall()
    return [dict(r) for r in rows]


def get_plate(conn: sqlite3.Connection, plate_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM plates WHERE plate_id = ?",
                       (plate_id,)).fetchone()
    return dict(row) if row else None


def load_wells(conn: sqlite3.Connection, plate_ids: list[str] | None = None) -> list[dict]:
    if plate_ids:
        marks = ",".join("?" for _ in plate_ids)
        rows = conn.execute(
            f"SELECT * FROM wells WHERE plate_id IN ({marks}) ORDER BY plate_id,row_idx,col_idx",
            plate_ids).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM wells ORDER BY plate_id,row_idx,col_idx").fetchall()
    return [dict(r) for r in rows]


def plate_dims(conn: sqlite3.Connection, plate_ids: list[str]) -> dict[str, tuple[int, int]]:
    marks = ",".join("?" for _ in plate_ids)
    rows = conn.execute(
        f"SELECT plate_id,n_rows,n_cols FROM plates WHERE plate_id IN ({marks})",
        plate_ids).fetchall()
    return {r["plate_id"]: (r["n_rows"], r["n_cols"]) for r in rows}


# ---- 分析快照（只插入，永不更新）-----------------------------------------
def save_analysis(conn: sqlite3.Connection, payload: dict) -> int:
    cur = conn.execute(
        """INSERT INTO analyses (created_at,plate_set,roles,method,method_choice,
               bad_well_k,suggested_bad,explicit_flags,used_keys,rank,rank_deficient,
               diagnostics,validation_result,summary,parent_analysis_id,note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_now(),
         json.dumps(payload["plate_set"], ensure_ascii=False),
         json.dumps(payload["roles"], ensure_ascii=False),
         payload["method"],
         json.dumps(payload["method_choice"], ensure_ascii=False),
         payload["bad_well_k"],
         json.dumps(payload["suggested_bad"], ensure_ascii=False),
         json.dumps(payload["explicit_flags"], ensure_ascii=False),
         json.dumps(payload["used_keys"], ensure_ascii=False),
         payload["rank"], int(payload["rank_deficient"]),
         json.dumps(payload["diagnostics"], ensure_ascii=False),
         json.dumps(payload["validation_result"], ensure_ascii=False)
         if payload.get("validation_result") is not None else None,
         json.dumps(payload["summary"], ensure_ascii=False),
         payload.get("parent_analysis_id"),
         payload.get("note", "")))
    analysis_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO analysis_wells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(analysis_id, w["plate_id"], w["row_idx"], w["col_idx"], w["treatment"],
          int(w["edge"]), w.get("replicate_group", ""),
          w["response"], w["fitted"], w["residual"],
          None if w["rstd"] is None or (isinstance(w["rstd"], float) and np_isnan(w["rstd"])) else w["rstd"],
          int(w["used_in_fit"]), int(w["suggested_bad"]), int(w["flagged_bad"]))
         for w in payload["well_rows"]])
    conn.executemany(
        """INSERT INTO analysis_effects VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(analysis_id, e["column"], e["family"], e["plate"], e["level"],
          int(e["estimable"]), e["estimate"], e["se"], e["ci_low"], e["ci_high"],
          e.get("note")) for e in payload["effect_rows"]])
    conn.executemany(
        """INSERT INTO analysis_contrasts VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(analysis_id, c["kind"], c["label"], int(c["estimable"]),
          c.get("estimate"), c.get("se"), c.get("ci_low"), c.get("ci_high"),
          c.get("note"), json.dumps(c.get("extra"), ensure_ascii=False)
          if c.get("extra") is not None else None)
         for c in payload["contrast_rows"]])
    conn.commit()
    return analysis_id


def np_isnan(x) -> bool:
    try:
        return x != x  # NaN 自检
    except Exception:
        return False


def list_analyses(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id,created_at,plate_set,method,rank,rank_deficient,"
        "parent_analysis_id,note,bad_well_k FROM analyses ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["plate_set"] = json.loads(d["plate_set"])
        out.append(d)
    return out


def get_analysis(conn: sqlite3.Connection, analysis_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM analyses WHERE id = ?",
                       (analysis_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("plate_set", "roles", "method_choice", "suggested_bad",
                "explicit_flags", "used_keys", "diagnostics", "summary"):
        d[key] = json.loads(d[key])
    d["validation_result"] = (json.loads(d["validation_result"])
                              if d["validation_result"] else None)
    d["wells"] = [dict(r) for r in conn.execute(
        "SELECT * FROM analysis_wells WHERE analysis_id=? ORDER BY plate_id,row_idx,col_idx",
        (analysis_id,))]
    d["effects"] = [dict(r) for r in conn.execute(
        "SELECT * FROM analysis_effects WHERE analysis_id=?", (analysis_id,))]
    d["contrasts"] = [dict(r) for r in conn.execute(
        "SELECT * FROM analysis_contrasts WHERE analysis_id=?", (analysis_id,))]
    return d
