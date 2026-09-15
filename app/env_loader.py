"""env_loader.py — 项目根 .env 加载（零依赖，不污染已有环境变量）

agent_runner 与 scripts/ 共用同一套解析逻辑，避免各自维护一份重复实现。
"""
from __future__ import annotations

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv() -> None:
    """从项目根 .env 注入环境变量（已有值不覆盖，无 python-dotenv 依赖）。"""
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
