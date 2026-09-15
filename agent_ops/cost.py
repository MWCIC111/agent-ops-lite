"""cost.py — 成本核算

按模型单价折算 token 用量为成本。
单价表与 app/demo_data.py 完全一致 —— 保证"采集 → 面板展示"成本口径统一。
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("agent_ops.cost")

# 每 1K token 价格（美元）：(input, output)
# 与 app/demo_data.py 的 MODEL_PRICE 保持一致（面板展示用同一套单价）
MODEL_PRICE: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.0100),
    "qwen-max": (0.0015, 0.0060),
    "qwen-plus": (0.0004, 0.0012),
    # DeepSeek 官方价（按汇率 7.2 折算美元/1K）
    "deepseek-chat": (0.00014, 0.00028),
    "deepseek-reasoner": (0.00028, 0.00112),
}

# 美元 → 人民币参考汇率（仅用于展示，可在接入生产时替换为实时汇率）
USD_TO_CNY = 7.2


def _warn_unknown(model: str) -> None:
    _LOG.warning("未知模型 %s 未在 MODEL_PRICE 中配置单价，按 0 价兜底", model)


def ensure_price(model: str) -> tuple[float, float]:
    """返回模型单价；未知模型打警告并按 (0,0) 兜底，避免成本静默失真。"""
    price = MODEL_PRICE.get(model)
    if price is None:
        _warn_unknown(model)
        return (0.0, 0.0)
    return price


def step_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """单步成本（美元）"""
    price = ensure_price(model)
    return (tokens_in * price[0] + tokens_out * price[1]) / 1000


def step_cost_cny(model: str, tokens_in: int, tokens_out: int) -> float:
    """单步成本（人民币）"""
    return step_cost_usd(model, tokens_in, tokens_out) * USD_TO_CNY
