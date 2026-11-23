#!/usr/bin/env python3
"""命令行入口：初始化合成数据并（可选）创建首批分析。

用法：
  .venv/bin/python -m scripts.cli init-db        # 导入三个场景
  .venv/bin/python -m scripts.cli analyze A      # 对 A 场景全部板建分析
  .venv/bin/python -m scripts.cli list
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analysis, storage, synthetic  # noqa: E402

SCENARIO_PREFIX = {
    "A": "A_random", "B": "B_row_confounded", "C": "C_edge_missing",
}


def cmd_init(db: str) -> None:
    conn = storage.connect(db)
    for scenario in synthetic.generate_all():
        for plate in scenario["plates"]:
            try:
                storage.import_plate(conn, plate)
                print(f"导入 {plate['plate_id']}（{plate['role']}）")
            except ValueError as exc:
                print(f"跳过 {plate['plate_id']}：{exc}")


def cmd_analyze(db: str, scenario: str, k: float) -> None:
    conn = storage.connect(db)
    name = SCENARIO_PREFIX[scenario]
    plate_ids = [p["plate_id"] for p in storage.list_plates(conn)
                 if p["scenario"] == name]
    if not plate_ids:
        print(f"场景 {name} 尚未导入，请先运行 init-db")
        sys.exit(1)
    aid = analysis.run_analysis(conn, plate_ids, k=k,
                                note=f"CLI 分析：{name}")
    print(f"已创建分析 #{aid}")
    print(json.dumps(storage.get_analysis(conn, aid)["summary"],
                     ensure_ascii=False, indent=2)[:4000])


def cmd_list(db: str) -> None:
    conn = storage.connect(db)
    for p in storage.list_plates(conn):
        print(f"{p['plate_id']:12s} {p['role']:11s} {p['scenario']:18s} #{p['import_seq']}")
    for a in storage.list_analyses(conn):
        print(f"分析 #{a['id']}: {a['method']} 秩={a['rank']} 板={a['plate_set']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init-db", "analyze", "list"])
    parser.add_argument("scenario", nargs="?", choices=list(SCENARIO_PREFIX), default="A")
    parser.add_argument("--db", default=storage.DB_PATH)
    parser.add_argument("--k", type=float, default=4.0)
    args = parser.parse_args()
    {"init-db": lambda: cmd_init(args.db),
     "analyze": lambda: cmd_analyze(args.db, args.scenario, args.k),
     "list": lambda: cmd_list(args.db)}[args.command]()


if __name__ == "__main__":
    main()
