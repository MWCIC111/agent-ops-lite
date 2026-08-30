"""app/op_log.py — 操作人员操作时间线（轻量 JSONL 持久化，用于时间追踪 / 审计）

每次「运行真实 Agent / 播种 / 清空数据 / 访问页面」都会留下真实时间戳，
可被「10_操作日志」页回看，满足「操作人员在什么时间做了什么」的追踪需求。

数据落在仓库根的 operations.log（单行 JSON），不在数据库中、不影响真实 Trace。
"""
from __future__ import annotations

import os
import json
import time
import datetime

_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "operations.log")
_LAST_ACCESS: dict = {}


def log_operation(page: str, action: str, detail: str = "",
                  throttle_key: str = None, throttle_sec: int = 0):
    """追加一条操作记录。

    throttle_key / throttle_sec：相同 throttle_key 在 throttle_sec 秒内只记一次，
    用于页面访问（Streamlit 每次交互都会重跑脚本）。
    """
    now = time.time()
    if throttle_key:
        if now - _LAST_ACCESS.get(throttle_key, 0.0) < throttle_sec:
            return
        _LAST_ACCESS[throttle_key] = now

    rec = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page": page,
        "action": action,
        "detail": detail,
    }
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # 记录失败不影响主流程
        pass


def load_operations(limit: int = 200):
    """读取最近 limit 条操作记录（旧->新）。"""
    if not os.path.exists(_LOG_PATH):
        return []
    out = []
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-limit:]
