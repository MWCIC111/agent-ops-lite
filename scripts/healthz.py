"""healthz.py — 轻量健康检查端点（零新依赖，仅标准库）

用于生产探针（K8s liveness/readiness、监控拨测）：
  python scripts/healthz.py --port 8080 --freshness-min 30

GET /healthz 返回 JSON：
  {
    "status": "ok" | "degraded" | "error",
    "db_reachable": true,
    "trace_count": 123,
    "last_trace_age_min": 12.3,   # null 表示库空
    "api_key_configured": true,
    "checked_at": "2026-09-07T09:00:00"
  }

判定：
  - db 不可达 / 异常            -> status=error（HTTP 503）
  - 库空 或 最近 Trace 超过新鲜度 -> status=degraded（HTTP 200，供告警区分）
  - 其余                         -> status=ok
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_ops.storage import SQLiteStore  # noqa: E402

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")


def build_report(freshness_min: float) -> dict:
    rep = {
        "status": "ok",
        "db_reachable": False,
        "trace_count": 0,
        "last_trace_age_min": None,
        "api_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        store = SQLiteStore(DB_PATH)
        rep["db_reachable"] = True
        rep["trace_count"] = store.count()
        latest = store.latest_started_at()
        if latest is not None:
            age = (datetime.now() - latest).total_seconds() / 60.0
            rep["last_trace_age_min"] = round(age, 1)
            if age > freshness_min:
                rep["status"] = "degraded"
        else:
            rep["status"] = "degraded"  # 无数据，视为亚健康
    except Exception as e:  # noqa: BLE001
        rep["status"] = "error"
        rep["error"] = str(e)
    if not rep["db_reachable"]:
        rep["status"] = "error"
    return rep


class _Handler(BaseHTTPRequestHandler):
    freshness_min = 30.0

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] not in ("/healthz", "/"):
            self.send_response(404)
            self.end_headers()
            return
        rep = build_report(_Handler.freshness_min)
        body = json.dumps(rep, ensure_ascii=False).encode("utf-8")
        code = 200 if rep["status"] != "error" else 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默默认访问日志
        return


def main() -> None:
    ap = argparse.ArgumentParser(description="agent-ops-lite 健康检查端点")
    ap.add_argument("--port", type=int, default=8080, help="监听端口")
    ap.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    ap.add_argument("--freshness-min", type=float, default=30.0,
                    help="最近 Trace 超过该分钟数即判定 degraded")
    args = ap.parse_args()

    _Handler.freshness_min = args.freshness_min
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"▶ healthz 监听 http://{args.host}:{args.port}/healthz "
          f"（新鲜度阈值 {args.freshness_min} 分钟）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹ healthz 已停止", flush=True)


if __name__ == "__main__":
    main()
