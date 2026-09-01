"""butler_fusion.py — 研发管家「置信度融合 + 三层幻觉抑制」(数值化)

把"置信度融合（相似度 + logprob + 业务规则）"落成可量化代码：
  - 相似度分量：检索 top BM25 分经 sigmoid 归一
  - logprob 分量：DeepSeek 真实 token logprob 均值经 sigmoid
  - 业务规则分量：答案是否含"不确定/无法/缺乏"等hedge 词 → 降权
最终置信度 = 0.4*相似度 + 0.35*logprob + 0.25*规则，门控阈值 GATE。

与服务器原版"让 LLM 自评高/中/低"的 prompt 式实现不同，这里是**结构化数值融合**，
可被门控、可被观测、可被回写闭环消费。
"""
from __future__ import annotations

import math
import os
from typing import Tuple

OPENAI_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

GATE = 0.50  # 低于此置信度 → 转人工审核队列

_HEDGE_WORDS = ("不确定", "无法", "缺乏", "没有足够", "不能回答", "无法回答", "不知道", "不足")


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _retrieval_component(top_bm25: float) -> float:
    """BM25 分（可正可负）经 sigmoid 映射到 0..1。top=0 → 0.5 基线。"""
    return _sigmoid(top_bm25 / 3.0)


def _rule_component(text: str) -> float:
    """业务规则：含 hedge 词 → 显著降权，否则满分。"""
    if any(w in text for w in _HEDGE_WORDS):
        return 0.30
    return 1.00


def _logprob_component(mean_logprob: float) -> float:
    """token logprob 均值（通常为负）经 sigmoid 映射到 0..1。"""
    return _sigmoid(mean_logprob)


def fusion_with_confidence(
    model: str,
    question: str,
    ctx: str,
    parts: list,
    top_bm25: float,
) -> Tuple[str, float, int, int, int]:
    """调用 DeepSeek 做融合+三层校验，返回 (文本, 置信度, in_tok, out_tok, 耗时ms)。

    优先使用真实 logprobs；若端点不支持/异常，则回退到"相似度+规则"启发式，
    保证在任何 DeepSeek 兼容端点下都能产出数值置信度。
    """
    import time

    from openai import OpenAI

    sys_prompt = (
        "你是置信度融合与幻觉抑制模块：综合各垂直 Agent 结论，做三层校验"
        "（工具层事实一致性 → LLM 层逻辑自洽 → 输出层与检索依据对齐），"
        "给出最终可信结论。"
    )
    user_content = (
        f"问题：{question}\n检索依据：{ctx[:1000]}\n各 Agent 结论：\n" + "\n\n".join(parts)
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
    ]

    t0 = time.perf_counter()
    client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=500,
            logprobs=True,
            top_logprobs=1,
        )
        text = resp.choices[0].message.content or ""
        tin = int(resp.usage.prompt_tokens or 0)
        tout = int(resp.usage.completion_tokens or 0)
        # 取生成 token 的 mean logprob
        mean_lp = -2.0
        lp_obj = getattr(resp.choices[0], "logprobs", None)
        if lp_obj and getattr(lp_obj, "content", None):
            vals = [
                t.top_logprobs[0].logprob
                for t in lp_obj.content
                if t.top_logprobs
            ]
            if vals:
                mean_lp = sum(vals) / len(vals)
        lp_comp = _logprob_component(mean_lp)
    except Exception:
        # 端点不支持 logprobs 或异常 → 回退：用规则构造一次普通生成
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0.3, max_tokens=500
        )
        text = resp.choices[0].message.content or ""
        tin = int(resp.usage.prompt_tokens or 0)
        tout = int(resp.usage.completion_tokens or 0)
        lp_comp = 0.5  # 未知 → 取中性

    ms = max(int((time.perf_counter() - t0) * 1000), 1)

    retr_comp = _retrieval_component(top_bm25)
    rule_comp = _rule_component(text)
    confidence = round(0.40 * retr_comp + 0.35 * lp_comp + 0.25 * rule_comp, 3)

    # 检索几乎零命中（无 token 重叠）→ 强制低置信，必转人工
    if top_bm25 <= 0.0:
        confidence = min(confidence, 0.20)

    return text, confidence, tin, tout, ms
