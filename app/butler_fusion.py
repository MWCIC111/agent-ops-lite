"""butler_fusion.py — 研发管家「置信度融合 + 三层幻觉抑制」(数值化)

把"置信度融合（相似度 + logprob + 业务规则）"落成可量化代码：
  - 相似度分量：检索命中的查询内相对信号 ratio = top1/mean(top_k) 经 sigmoid 归一
  - logprob 分量：DeepSeek 真实 token logprob 均值经 sigmoid
  - 业务规则分量：答案是否含"不确定/无法/缺乏"等hedge 词 → 降权
最终置信度 = 0.4*相似度 + 0.35*logprob + 0.25*规则，门控阈值 GATE。
检索层另有独立硬门控（abs 与 ratio 两个信号都报警 → 强制低置信），见下方常量区实测说明。

与服务器原版"让 LLM 自评高/中/低"的 prompt 式实现不同，这里是**结构化数值融合**，
可被门控、可被观测、可被回写闭环消费。
"""
from __future__ import annotations

import math
import os
import re
from typing import Tuple

import openai

GATE = 0.50  # 低于此置信度 → 转人工审核队列

# ---- 检索层硬门控参数（2026-09-13 校订，scripts/eval_gate.py --natural 可复现）------
# ⚠ 这条注释是结论，不是过程。同一类坑踩了三次：
#   [坑1] 原实现 _sigmoid(top_bm25 / 3.0)：BM25Okapi 原始分在 10~120 量级，分母 3.0
#         使分量恒饱和 ≈1.0（库内/库外拿到同一分量，AUC 0.517 ≈ 抛硬币）；
#         配套「top_bm25 <= 0 → 强制低置信」因分数恒为正而永不触发（死代码，实测 0/20）。
#   [坑2] 改用查询内相对信号 ratio = top1/mean(top_k) 后，通用域（华佗）AUC 0.983 看着很好；
#         换到 IVD 语料（gold 集≈54 块）AUC 掉到 0.168 —— 因为一个答案由多块共同承载时
#         top4 全部命中同一文档、分数彼此接近，ratio 恒等于 1.0，信号自毁。
#   [坑3 · 最要命] 换成 top1 绝对分后，IVD 语料 easy 档 AUC=1.000、「库内保留 100%」，
#         但它是**查询风格的产物**：easy 档正例取自文档标题逐字（top1 min 30.32），
#         而人工撰写的口语化库内问题 top1 只有 15.4~37.6，与库外（0~29.3）**完全重叠**。
#         按那个阈值（25.4）上线，实测误拦 13/20 = 65% 的真实库内提问。
#
# 结论（决定当前默认值）：**BM25 分数在本语料上不具备「库内 / 库外判别」能力。**
# 因此检索层门控**降级为「极端无支撑熔断」而非判别器**：
#   · 只拦「两个信号同时报警」的极端情形（AND），实测库内误拦 0%（20/20 保留）；
#   · 库外拦截只覆盖 2/21 ≈ 10%，其余交给后续两层（LLM 自评 + 输出层对齐 + hedge 规则）；
#   · ABS_GATE 默认取 12.0（而非按 easy 档选出的 25.4）——宁可基本不拦，也不误伤库内。
# 灵敏度（scripts/eval_gate.py --natural 可复现）：
#   ABS_GATE=25.4 → 库内保留 35%（误拦 13/20）｜库外拦截 76%   ← easy 档选点，已弃用
#   ABS_GATE=15.0 → 库内保留 100%            ｜库外拦截 29%
#   ABS_GATE=12.0 → 库内保留 100%            ｜库外拦截 10%   ← 当前默认（保守）
# 待办：判断能否提高需 hard 档（口语化正例的规模化版本）后重新标定。
# 阈值可用环境变量覆盖，便于换语料后免改代码重标定：RAG_ABS_GATE / RAG_RATIO_GATE。
RETR_MID = 1.14    # ratio → 分量 的映射中点（软信号，进置信度公式）
RETR_SCALE = 0.09  # 映射陡度
ABS_GATE = float(os.environ.get("RAG_ABS_GATE", "12.0"))       # top1 绝对分下限（极端无支撑熔断）
RATIO_GATE = float(os.environ.get("RAG_RATIO_GATE", "1.14"))   # ratio 下限（AND 的另一条件）

_HEDGE_WORDS = ("不确定", "无法", "缺乏", "没有足够", "不能回答", "无法回答", "不知道", "不足")

# 兜底强否定词表（仅当模型未按"判定：可信/不可信"格式输出时，检查文本开头 40 字）：
# 实测（2026-09-03）：库外问题下 DeepSeek 输出几乎恒定以"无法基于/无法给出/不可信"开头；
# 用全文词表会误伤正常答案里的"检索片段相关性评估表"（表格内写"该片段与问题无关"是评估过程，
# 不是最终判定），故只做开头快检兜底，主信号是首行结构化判定。
_ALIGN_FAIL = ("无法基于", "无法给出", "无法回答", "不能回答", "不可信", "拒绝", "抱歉", "无法提供")


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _retrieval_signal(hits: list) -> tuple:
    """由命中的 BM25 分数算「检索支撑度」，返回 (分量 0..1, ratio, top1)。

    ratio = top1 / mean(全部命中分数)，是查询内相对信号：为什么不用绝对分，
    见上方常量区的实测记录（绝对分 AUC 0.517 ≈ 抛硬币，ratio 0.982）。
    命中为空、或分数全为 0（无 token 重叠）→ 视为无支撑，分量取 0。
    """
    scores = [float(h.get("score", 0.0) or 0.0) for h in hits if h]
    if not scores:
        return 0.0, 0.0, 0.0
    top1 = max(scores)
    mean = sum(scores) / len(scores)
    if mean <= 1e-9:
        return 0.0, 0.0, 0.0
    ratio = top1 / mean
    return _sigmoid((ratio - RETR_MID) / RETR_SCALE), ratio, top1


def _rule_component(text: str) -> float:
    """业务规则：含 hedge 词 → 显著降权，否则满分。"""
    if any(w in text for w in _HEDGE_WORDS):
        return 0.30
    return 1.00


def _logprob_component(mean_logprob: float) -> float:
    """token logprob 均值（通常为负）经 sigmoid 映射到 0..1。"""
    return _sigmoid(mean_logprob)


def _extract_verdict(text: str) -> bool | None:
    """解析融合模块首行的结构化判定。

    返回 True=可信 / False=不可信 / None=未按格式输出。
    用显式判定行而不是全文词表匹配，是为了把"输出层与检索依据对齐"变成
    模型必须表态的结构化信号——全文搜"无关/无法"会误伤正常答案里的
    "检索片段相关性评估"过程描述（2026-09-03 实测踩坑）。
    """
    head = (text or "").strip()
    if not head:
        return None
    m = re.search(r"判定\s*[:：]\s*(可信|不可信)", head.splitlines()[0])
    if m:
        return m.group(1) == "可信"
    return None


def fusion_with_confidence(
    model: str,
    question: str,
    ctx: str,
    parts: list,
    hits: list,
) -> Tuple[str, float, int, int, int]:
    """调用 DeepSeek 做融合+三层校验，返回 (文本, 置信度, in_tok, out_tok, 耗时ms)。

    hits：检索命中列表（含 score），用于算检索支撑度 ratio = top1/mean(top_k)。
    优先使用真实 logprobs；若端点不支持/异常，则回退到"相似度+规则"启发式，
    保证在任何 DeepSeek 兼容端点下都能产出数值置信度。
    """
    import time

    from agent_runner import _deepseek_chat, chat_with_logprobs

    sys_prompt = (
        "你是置信度融合与幻觉抑制模块：综合各垂直 Agent 结论，做三层校验"
        "（工具层事实一致性 → LLM 层逻辑自洽 → 输出层与检索依据对齐）。\n"
        "输出要求：第一行必须是判定行，格式严格为——\n"
        "判定：可信   （当检索依据足以支撑回答、且三层校验通过时）\n"
        "判定：不可信 （当检索依据与问题不对齐 / 信息不足 / 各 Agent 结论不可靠时）\n"
        "第一行之后输出详细校验过程与最终可信结论。"
    )
    user_content = (
        f"问题：{question}\n检索依据：{ctx[:1000]}\n各 Agent 结论：\n" + "\n\n".join(parts)
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
    ]

    t0 = time.perf_counter()
    try:
        text, tin, tout, mean_lp = chat_with_logprobs(
            model, messages, temperature=0.3, max_tokens=500
        )
    except openai.OpenAIError:
        # 兼容端点不支持 logprobs → 回退普通生成（仍走统一配额/超时/重试）
        text, tin, tout = _deepseek_chat(
            model, messages, temperature=0.3, max_tokens=500
        )
        mean_lp = None

    lp_comp = _logprob_component(mean_lp) if mean_lp is not None else 0.5

    ms = max(int((time.perf_counter() - t0) * 1000), 1)

    retr_comp, ratio, top1 = _retrieval_signal(hits)
    rule_comp = _rule_component(text)
    # 输出层对齐校验：模型首行判定"不可信" → 乘性强惩罚 ×0.35，
    # 使"检索依据与问题不对齐/硬编回答"必然跌破 GATE 转人工（2026-09-03 实测校准）。
    verdict = _extract_verdict(text)
    if verdict is False:
        align_ok = False
    elif verdict is True:
        align_ok = True
    else:
        # 模型未按格式输出 → 兜底：只看文本开头 40 字内的强否定信号（避免误伤评估过程描述）
        align_ok = not any(w in text.strip()[:40] for w in _ALIGN_FAIL)
    confidence = round(
        (0.40 * retr_comp + 0.35 * lp_comp + 0.25 * rule_comp) * (1.0 if align_ok else 0.35),
        3,
    )

    # 检索层硬门控：**两个信号都报警**才判「无检索支撑」，强制低置信转人工（AND，保守）。
    # 单用 ratio 会在 gold 集大的语料上失效（实测 IVD 语料库内保留率 0.0%），单用绝对分
    # 则受语料量级与查询风格影响；AND 对单信号失效有韧性，且优先不误拦库内。
    # 阈值来源、噪声底检查与未验证项见上方常量区；统计口径见 scripts/eval_gate.py。
    if ratio < RATIO_GATE and top1 < ABS_GATE:
        confidence = min(confidence, 0.20)

    return text, confidence, tin, tout, ms
