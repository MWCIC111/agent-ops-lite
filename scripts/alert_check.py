"""alert_check.py — 真实告警推送（Phase 1d）

读取 agent_ops.db 聚合指标，错误率 / 成本超阈值时向企业微信 / 飞书机器人真实推送。
与 scripts/seed_real_data.py 同节奏挂 cron 即可（如每 5~10 分钟一次）。

  python scripts/alert_check.py
  AGENTOPS_WEBHOOK_URL=https://qyapi.weixin.qq.com/... AGENTOPS_WEBHOOK_TYPE=wecom \
      python scripts/alert_check.py

未配置 AGENTOPS_WEBHOOK_URL 时仅打印聚合指标（不推送），便于本地验证逻辑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_dotenv() -> None:
    """从项目根 .env 注入环境变量（不依赖 python-dotenv）。"""
    dotenv = os.path.join(_REPO_ROOT, ".env")
    if not os.path.exists(dotenv):
        return
    with open(dotenv, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and os.environ.get(key) is None:
                os.environ[key] = value


_load_dotenv()

from agent_ops.storage import SQLiteStore  # noqa: E402
from agent_ops.tracer import Collector  # noqa: E402
from agent_ops.metrics import report  # noqa: E402
from agent_ops.alerts import WebhookAlert, DEFAULT_RULES  # noqa: E402

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")


def main() -> None:
    ap = argparse.ArgumentParser(description="读取指标并真实推送告警")
    ap.add_argument("--since-hours", type=float, default=0.0,
                    help="仅统计最近 N 小时的 Trace（0=全部）")
    args = ap.parse_args()

    collector = Collector(storage=SQLiteStore(DB_PATH))
    since = None
    if args.since_hours > 0:
        from datetime import datetime, timedelta
        since = datetime.now() - timedelta(hours=args.since_hours)
    rep = report(collector, since=since)

    webhook = os.environ.get("AGENTOPS_WEBHOOK_URL", "")
    wtype = os.environ.get("AGENTOPS_WEBHOOK_TYPE", "wecom")
    if not webhook:
        print("[alert] 未配置 AGENTOPS_WEBHOOK_URL，仅打印聚合指标：")
        print(json.dumps(rep["total"], ensure_ascii=False, indent=2))
        return

    alert = WebhookAlert(webhook_url=webhook, webhook_type=wtype, rules=DEFAULT_RULES)
    fired = alert.check_and_send(rep)
    if fired:
        print(f"[alert] 触发并推送 {len(fired)} 条告警：")
        for ev in fired:
            print("  - " + ev.render().replace("\n", " | "))
    else:
        print(f"[alert] 指标正常（{rep['window']}），无告警。")


if __name__ == "__main__":
    main()
