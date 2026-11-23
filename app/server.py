"""本地 HTTP 服务：标准库实现，无第三方 Web 依赖。

API：
GET  /api/health
GET  /api/plates                      板列表（含角色、场景）
GET  /api/plates/<id>                 单板原始孔位（原始记录，不含坏孔字段）
POST /api/import/scenario             导入一个合成场景（body: {"scenario":"A_random|..."}）
POST /api/import/generate             导入全部三个场景
GET  /api/analyses                    分析版本列表
POST /api/analyses                    用当前板集合创建分析
GET  /api/analyses/<id>               分析快照完整内容（不可变）
POST /api/analyses/<id>/rerun         翻转坏孔标记 → 新版本
GET  /api/scenarios                   场景说明与真值（合成数据核对用）
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import analysis, storage, synthetic

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")

SCENARIO_FUNCS = {
    "A_random": synthetic.generate_scenario_a,
    "B_row_confounded": synthetic.generate_scenario_b,
    "C_edge_missing": synthetic.generate_scenario_c,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PlateStats/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---- 基础工具 ----
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel: str):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(os.path.splitext(path)[1],
                                              "application/octet-stream")
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _conn(self):
        # 每个工作线程一个独立连接（SQLite 连接不能跨线程共享）
        local = self.server.thread_local  # type: ignore[attr-defined]
        if not hasattr(local, "conn"):
            local.conn = storage.connect(self.server.db_path)  # type: ignore[attr-defined]
        return local.conn

    # ---- 路由 ----
    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts or parts[0] == "index.html":
                return self._send_file("index.html")
            if parts[0] == "static":
                return self._send_file(os.path.join(*parts[1:]))
            if parts[0] != "api":
                return self._send_file(url.path.lstrip("/"))
            if parts == ["api", "health"]:
                return self._send_json({"status": "ok"})
            if parts == ["api", "plates"]:
                plates = storage.list_plates(self._conn())
                for p in plates:
                    p["n_wells"] = self._conn().execute(
                        "SELECT COUNT(*) c FROM wells WHERE plate_id=?",
                        (p["plate_id"],)).fetchone()["c"]
                return self._send_json(plates)
            if len(parts) == 3 and parts[:2] == ["api", "plates"]:
                plate = storage.get_plate(self._conn(), parts[2])
                if not plate:
                    return self._send_json({"error": "板不存在"}, 404)
                plate["wells"] = storage.load_wells(self._conn(), [parts[2]])
                return self._send_json(plate)
            if parts == ["api", "analyses"]:
                return self._send_json(storage.list_analyses(self._conn()))
            if len(parts) == 3 and parts[:2] == ["api", "analyses"]:
                data = storage.get_analysis(self._conn(), int(parts[2]))
                if not data:
                    return self._send_json({"error": "分析不存在"}, 404)
                return self._send_json(data)
            if parts == ["api", "scenarios"]:
                return self._send_json([
                    {"scenario": s["scenario"], "description": s["description"],
                     "ground_truth": s["ground_truth"],
                     "plates": [{"plate_id": p["plate_id"], "role": p["role"]}
                                for p in s["plates"]]}
                    for s in synthetic.generate_all()])
            return self._send_json({"error": "未知路由"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if parts == ["api", "import", "scenario"]:
                body = self._read_json()
                func = SCENARIO_FUNCS.get(body.get("scenario", ""))
                if not func:
                    return self._send_json({"error": "未知场景"}, 400)
                scenario = func()
                imported = []
                for plate in scenario["plates"]:
                    try:
                        storage.import_plate(self._conn(), plate)
                        imported.append(plate["plate_id"])
                    except ValueError as exc:
                        return self._send_json({"error": str(exc),
                                                "imported": imported}, 409)
                return self._send_json({"imported": imported,
                                        "scenario": scenario["scenario"]})
            if parts == ["api", "import", "generate"]:
                result = {}
                for key, func in SCENARIO_FUNCS.items():
                    scenario = func()
                    done = []
                    for plate in scenario["plates"]:
                        try:
                            storage.import_plate(self._conn(), plate)
                            done.append(plate["plate_id"])
                        except ValueError:
                            pass  # 已导入则跳过
                    result[key] = done
                return self._send_json(result)
            if parts == ["api", "analyses"]:
                body = self._read_json()
                plate_ids = body.get("plate_ids")
                if not plate_ids:
                    plate_ids = [p["plate_id"] for p in
                                 storage.list_plates(self._conn())]
                if not plate_ids:
                    return self._send_json({"error": "尚无导入的板"}, 400)
                aid = analysis.run_analysis(
                    self._conn(), plate_ids, k=float(body.get("k", 4.0)),
                    explicit_flags=body.get("explicit_flags"),
                    note=body.get("note", ""))
                return self._send_json({"analysis_id": aid}, 201)
            if (len(parts) == 4 and parts[:2] == ["api", "analyses"]
                    and parts[3] == "rerun"):
                body = self._read_json()
                flags = body.get("explicit_flags")
                if not isinstance(flags, dict):
                    return self._send_json({"error": "需要 explicit_flags 映射"}, 400)
                aid = analysis.rerun_with_flags(
                    self._conn(), int(parts[2]), flags,
                    note=body.get("note", ""))
                return self._send_json({"analysis_id": aid}, 201)
            return self._send_json({"error": "未知路由"}, 404)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)


def make_server(host: str = "127.0.0.1", port: int = 8000,
                db_path: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path or storage.DB_PATH  # type: ignore[attr-defined]
    server.thread_local = threading.local()  # type: ignore[attr-defined]
    return server


def main():
    import argparse

    parser = argparse.ArgumentParser(description="实验板空间效应分析本地应用")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=storage.DB_PATH)
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"实验板分析应用已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
