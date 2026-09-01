"""butler_graph.py — 研发管家 真·LangGraph 多 Agent 编排

架构（与原"LangGraph 式"手搓版的本质区别：这里是真实 StateGraph + Send 扇出）：

    START → retrieve(共享State检索)
          → orchestrate(Orchestrator 集中式编排)
          → [Send ×4] vertical(4 垂直 Agent 并行，写入共享 State)
          → record_agents(按固定字典序落 4 步 Trace，保证确定性顺序)
          → fusion(置信度融合 + 三层幻觉抑制 + 数值门控)
          → 门控：低置信 → human_review(入审核队列) → END
                         高置信 → END

对外接口：run_butler(question, model) -> dict（含 answer/confidence/need_human/review_id），
供 agent_runner.run_research_butler 调用，保持 UI 消费方式不变。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Annotated, TypedDict

import operator

# ---- 路径：让本模块能 import 到 agent_runner（复用 _deepseek_chat / _retrieve_context） ----
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from agent_runner import _deepseek_chat, _retrieve_context, _LAST_HITS, collector
from agent_ops import record_step
from butler_fusion import fusion_with_confidence, GATE
from butler_review import add as review_add

BUTLER_AGENTS = {
    "抗原设计 Agent": "你是研发管家系统中的「抗原设计」垂直 Agent。基于检索依据与共享状态，给出抗原/表位设计方案（序列倾向、免疫原性权衡、设计风险点）。只输出你的专业结论。",
    "方案规划 Agent": "你是「方案规划」垂直 Agent。把设计任务拆为可执行的研发/实验方案，输出阶段、依赖、排期与验收口径。只输出你的专业结论。",
    "故障诊断 Agent": "你是「故障诊断」垂直 Agent。定位研发链路中的异常、瓶颈与风险，给出根因假设与对策。只输出你的专业结论。",
    "资料整理 Agent": "你是「资料整理」垂直 Agent。汇总前述各 Agent 的结论，整理为结构化交付报告（背景 / 方案 / 风险 / 下一步）。只输出你的专业结论。",
}


# ---------------------------------------------------------------------------
# 共享 State
# ---------------------------------------------------------------------------
class ButlerState(TypedDict):
    question: str
    model: str
    context: str
    hits: list
    orchestration: str
    # 4 个垂直 Agent 的结果列表（Send 扇出并行写入，用 operator.add 累积）
    agent_results: Annotated[list, operator.add]
    fusion_text: str
    confidence: float
    need_human: bool
    review_id: str | None
    answer: str


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
def _timed(fn):
    t0 = time.perf_counter()
    result = fn()
    ms = max(int((time.perf_counter() - t0) * 1000), 1)
    if isinstance(result, tuple):
        return *result, ms
    return result, ms


def retrieve_node(state: ButlerState) -> dict:
    ctx, hits = _retrieve_context(state["question"], top_k=4)
    _LAST_HITS[:] = hits
    record_step("共享State · 知识检索", model=state["model"],
                tool="BM25检索(华佗百科)", tokens_in=0, tokens_out=0, latency_ms=1)
    return {"context": ctx, "hits": hits}


def orchestrate_node(state: ButlerState) -> dict:
    msgs = [
        {"role": "system", "content": "你是研发管家的 Orchestrator，集中式编排 4 个垂直 Agent"
         "（抗原设计 / 方案规划 / 故障诊断 / 资料整理）。基于问题与检索依据，向每个 Agent 下发结构化子任务指令。"},
        {"role": "user", "content": f"问题：{state['question']}\n检索依据摘要：\n{state['context'][:1500]}"},
    ]
    orch, oin, oout, oms = _timed(lambda: _deepseek_chat(state["model"], msgs))
    record_step("Orchestrator · 任务编排", model=state["model"],
                tokens_in=oin, tokens_out=oout, latency_ms=oms)
    return {"orchestration": orch}


def fanout(state: ButlerState) -> list:
    """Orchestrator 后扇出 4 个垂直 Agent（真实 LangGraph Send 动态分发）。

    注：Send 的 payload 即目标节点的输入 state，不会自动携带全程共享 State，
    故把 question/model/context/orchestration 一并带入，供垂直 Agent 使用。
    """
    return [
        Send("vertical", {
            "name": n,
            "sys_prompt": sp,
            "question": state["question"],
            "model": state["model"],
            "context": state["context"],
            "orchestration": state["orchestration"],
        })
        for n, sp in BUTLER_AGENTS.items()
    ]


def vertical_node(state: ButlerState) -> dict:
    """单个垂直 Agent：基于共享 State（context / orchestration）+ 自身指令生成结论。"""
    name = state["name"]
    sys_prompt = state["sys_prompt"]
    msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content":
            f"共享状态：\n- 问题：{state['question']}\n- 检索依据：{state['context'][:1000]}\n"
            f"- Orchestrator 指令：{state['orchestration'][:600]}\n请基于以上输出你的专业结论。"},
    ]
    ans, tin, tout, ms = _timed(lambda: _deepseek_chat(state["model"], msgs, max_tokens=400))
    return {"agent_results": [{"name": name, "ans": ans, "tin": tin, "tout": tout, "ms": ms}]}


def record_agents_node(state: ButlerState) -> dict:
    """按固定字典序落 4 步 Trace（保证 7 步顺序确定性，与原 verify_butler 断言一致）。"""
    by_name = {r["name"]: r for r in state["agent_results"]}
    for name, _ in BUTLER_AGENTS.items():
        r = by_name.get(name, {"ans": "", "tin": 0, "tout": 0, "ms": 1})
        record_step(name, model=state["model"], tool="垂直Agent·DeepSeek",
                    tokens_in=r["tin"], tokens_out=r["tout"], latency_ms=r["ms"])
    return {}


def fusion_node(state: ButlerState) -> dict:
    """置信度融合 + 三层幻觉抑制 + 数值门控 + （低置信）入审核队列。"""
    parts = []
    for name, _ in BUTLER_AGENTS.items():
        for r in state["agent_results"]:
            if r["name"] == name:
                parts.append(f"### {name}\n{r['ans']}")
                break
    top_bm25 = max((h.get("score", 0.0) for h in state["hits"]), default=0.0)
    text, confidence, tin, tout, ms = fusion_with_confidence(
        state["model"], state["question"], state["context"], parts, top_bm25
    )
    record_step("置信度融合 · 三层幻觉抑制", model=state["model"],
                tool="融合+三层幻觉抑制", tokens_in=tin, tokens_out=tout, latency_ms=ms)

    need_human = confidence < GATE
    review_id = None
    if need_human:
        review_id = review_add(state["question"], text, confidence)

    full = (
        f"## 研发管家 · 多Agent编排结果\n\n{text}\n\n---\n\n" + "\n\n".join(parts)
        + f"\n\n> 置信度：{confidence:.2f}" + ("（低于阈值，已转人工审核队列）" if need_human else "")
    )
    return {"fusion_text": text, "confidence": confidence,
            "need_human": need_human, "review_id": review_id, "answer": full}


def human_review_node(state: ButlerState) -> dict:
    record_step("人工审核回写 · 已转人工队列", model=state["model"],
                tool="review_queue", tokens_in=0, tokens_out=0, latency_ms=1)
    return {}


def _route_after_fusion(state: ButlerState):
    return "human_review" if state["need_human"] else END


# ---------------------------------------------------------------------------
# 编译图（模块加载时构建一次）
# ---------------------------------------------------------------------------
def _build():
    g = StateGraph(ButlerState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("orchestrate", orchestrate_node)
    g.add_node("vertical", vertical_node)
    g.add_node("record_agents", record_agents_node)
    g.add_node("fusion", fusion_node)
    g.add_node("human_review", human_review_node)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "orchestrate")
    g.add_conditional_edges("orchestrate", fanout)  # 返回 Send 列表扇出
    g.add_edge("vertical", "record_agents")
    g.add_edge("record_agents", "fusion")
    g.add_conditional_edges("fusion", _route_after_fusion,
                            {"human_review": "human_review", END: END})
    g.add_edge("human_review", END)
    return g.compile()


GRAPH = _build()


def run_butler(question: str, model: str) -> dict:
    """入口：返回 {answer, confidence, need_human, review_id}。"""
    result = GRAPH.invoke({
        "question": question,
        "model": model,
        "context": "",
        "hits": [],
        "orchestration": "",
        "agent_results": [],
        "fusion_text": "",
        "confidence": 0.0,
        "need_human": False,
        "review_id": None,
        "answer": "",
    })
    return {
        "answer": result.get("answer", ""),
        "confidence": result.get("confidence", 0.0),
        "need_human": result.get("need_human", False),
        "review_id": result.get("review_id"),
    }
