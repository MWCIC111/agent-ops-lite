"""cost.py — 成本核算

按模型单价折算 token 用量为成本。
单价表与 app/demo_data.py 完全一致 —— 保证"采集 → 面板展示"成本口径统一。
"""
from __future__ import annotations

# 每 1K token 价格（美元）：(input, output)
# 与 app/demo_data.py 的 MODEL_PRICE 保持一致（面板展示用同一套单价）
MODEL_PRICE: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.0100),
    "qwen-max": (0.0015, 0.0060),
    "qwen-plus": (0.0004, 0.0012),
}

# 美元 → 人民币参考汇率（仅用于展示，可在接入生产时替换为实时汇率）
USD_TO_CNY = 7.2


def step_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """单步成本（美元）"""
    price = MODEL_PRICE.get(model)
    if price is None:
        raise KeyError(f"未知模型: {model}，请在 MODEL_PRICE 中补充单价")
    return (tokens_in * price[0] + tokens_out * price[1]) / 1000


def step_cost_cny(model: str, tokens_in: int, tokens_out: int) -> float:
    """单步成本（人民币）"""
    return step_cost_usd(model, tokens_in, tokens_out) * USD_TO_CNY
