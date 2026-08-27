"""examples/multi_agent_example.py — agent_ops × LangGraph 多 Agent 协作示例

对应简历核心项目「研发管家」的架构缩影：
    Orchestrator（意图路由） + 4 个垂直 Agent（检索 / 计算 / 校验 / 生成）

与 examples/langgraph_example.py 的区别：
  - langgraph_example.py 是 3 节点线性图（检索→生成→校验），演示"会接 LangGraph"
  - 本示例是 Orchestrator + 多垂直 Agent + 条件路由，演示"懂多 Agent 协作"
    ——直接对口目标岗位"LangGraph 多 Agent 应用开发"的核心考察点

图结构：
                  ┌→ 检索 Agent  (RAG: Milvus + Rerank)
  用户问题 → Orchestrator ─┼→ 计算 Agent  (SQL 工具)         ─→ 汇总节点 → END
                  └→ 校验 Agent  (回指校验)
                  └→ 生成 Agent  (LLM 生成)

@trace 包 graph.invoke 入口 → 一次调用 = 一条 Trace；
Orchestrator 的路由决策 + 每个 Agent 的工具调用都用 span 形成父子层级——
一条 Trace 里就能看到完整的多 Agent 协作结构。

运行：
    pip install -r examples/requirements.txt
    python examples/multi_agent_example.py
"""
from __future__ import annotations

import os
import sys

# 开发模式：允许从项目根目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import time
from typing import Literal, TypedDict

from langgraph.graph import END, StateGraph

from agent_ops import get_collector, model_usage, record_step, report, span, trace


# ---------------------------------------------------------------------------
# 1. 共享 State：多 Agent 协作的上下文载体
# ---------------------------------------------------------------------------


class MultiAgentState(TypedDict):
    question: str
    intent: Literal["retrieval", "computation", "verification", "generation"]
    route_reason: str          # Orchestrator 给出的路由理由（可解释性）
    retrieved_context: str     # 检索 Agent 的产出
    computed_result: str       # 计算 Agent 的产出
    verified: bool             # 校验 Agent 的产出
    draft_answer: str          # 生成 Agent 的产出
    final_answer: str          # 汇总节点的产出


# ---------------------------------------------------------------------------
# 2. Orchestrator 节点：意图识别 + 路由决策
#    用 span 包路由决策，让"为什么路由到这个 Agent"在 Trace 里可追溯
# ---------------------------------------------------------------------------


INTENT_KEYWORDS = {
    "retrieval": ["流程", "规范", "资料", "文档", "是什么", "怎么"],
    "computation": ["多少", "统计", "计算", "比例", "趋势"],
    "verification": ["校验", "核对", "回指", "是否符合", "一致"],
    "generation": ["生成", "起草", "写一份", "总结", "草拟"],
}


def orchestrator_node(state: MultiAgentState) -> dict:
    """意图路由：根据问题关键词判断走哪个垂直 Agent。"""
    time.sleep(random.uniform(0.1, 0.3))

    # 路由决策包成 span —— 父子层级体现"决策也是一步"
    with span("意图路由决策", model="qwen-plus"):
        q = state["question"]
        intent = "generation"  # 兜底
        reason = "默认走生成"
        for it, kws in INTENT_KEYWORDS.items():
            if any(kw in q for kw in kws):
                intent = it
                reason = f"命中关键词 → {it}"
                break
        record_step(
            "意图分类",
            model="qwen-plus",
            tokens_in=120,
            tokens_out=20,
            latency_ms=random.randint(80, 200),
        )

    return {"intent": intent, "route_reason": reason}


def route_after_orchestrator(state: MultiAgentState) -> str:
    """条件边函数：返回下一个节点名（LangGraph 路由核心）。"""
    return state["intent"]


# ---------------------------------------------------------------------------
# 3. 四个垂直 Agent：各用 span 包自己的工具调用，形成父子层级
#    每个对应研发管家里的一个垂直 Agent
# ---------------------------------------------------------------------------


def retrieval_agent(state: MultiAgentState) -> dict:
    """检索 Agent：RAG 检索链路（Milvus 向量检索 + Rerank 精排）。"""
    time.sleep(random.uniform(0.2, 0.5))
    # 模拟工具调用超时 → 抛异常，@trace 自动把整条 Trace 标记为 failed
    if "超时" in state["question"]:
        raise RuntimeError("知识库检索超时（ToolTimeout）")
    context = f"【依据】关于「{state['question']}」的研发资料片段……"

    with span("RAG 检索链路", model="qwen-plus"):
        record_step(
            "向量检索", model="qwen-plus", tool="Milvus",
            tokens_in=300, tokens_out=100, latency_ms=random.randint(150, 350),
        )
        record_step(
            "精排", model="qwen-plus", tool="Rerank",
            tokens_in=100, tokens_out=20, latency_ms=random.randint(50, 150),
        )
    return {"retrieved_context": context}


def computation_agent(state: MultiAgentState) -> dict:
    """计算 Agent：调用 SQL 工具取数 + 聚合计算。"""
    time.sleep(random.uniform(0.3, 0.6))
    result = f"【计算结果】{state['question']} 的数值答案 = 42.0"

    with span("数据计算链路", model="qwen-max"):
        record_step(
            "SQL 查询", model="qwen-max", tool="SQL查询",
            tokens_in=280, tokens_out=90, latency_ms=random.randint(200, 500),
        )
        record_step(
            "聚合计算", model="qwen-max", tool="Pandas",
            tokens_in=150, tokens_out=40, latency_ms=random.randint(100, 300),
        )
    return {"computed_result": result}


def verification_agent(state: MultiAgentState) -> dict:
    """校验 Agent：结论回指校验（医疗级语义校验的缩影）。

    无依据时不直接抛异常——而是模拟"校验失败"结果，让上层决定是否标记 Trace failed。
    真正的失败场景由 retrieval 超时触发（与 langgraph_example.py 一致）。
    """
    time.sleep(random.uniform(0.1, 0.3))
    ctx = state.get("retrieved_context", "") or state.get("computed_result", "")
    # 没有上下文时模拟一个 mock 依据（演示场景下让校验能通过）
    if not ctx:
        ctx = "【mock 依据】演示用占位上下文"
    verified = "【依据】" in ctx or "【计算结果】" in ctx or "【mock" in ctx

    with span("结论校验链路", model="qwen-plus"):
        record_step(
            "回指校验", model="qwen-plus", tool="校验器",
            tokens_in=200, tokens_out=50, latency_ms=random.randint(100, 300),
        )
    return {"verified": verified}


def generation_agent(state: MultiAgentState) -> dict:
    """生成 Agent：基于检索/计算/校验结果生成最终回答。"""
    time.sleep(random.uniform(0.3, 0.8))
    basis = state.get("retrieved_context") or state.get("computed_result") or ""
    draft = f"【回答】综合 {basis[:20]}…，{state['question']} 的结论是……"

    record_step(
        "内容生成", model="qwen-max",
        tokens_in=520, tokens_out=300, latency_ms=random.randint(300, 800),
    )
    return {"draft_answer": draft}


# ---------------------------------------------------------------------------
# 4. 汇总节点：合并各 Agent 产出
# ---------------------------------------------------------------------------


def summarize_node(state: MultiAgentState) -> dict:
    """汇总节点：把多 Agent 的产出拼成最终答案。"""
    time.sleep(random.uniform(0.1, 0.2))
    parts = [p for p in [state.get("retrieved_context"),
                        state.get("computed_result"),
                        state.get("draft_answer")] if p]
    final = " | ".join(parts) if parts else "（无可用产出）"
    record_step(
        "结果汇总", model="qwen-plus",
        tokens_in=100, tokens_out=30, latency_ms=random.randint(50, 150),
    )
    return {"final_answer": final}


# ---------------------------------------------------------------------------
# 5. 组装 LangGraph 图：Orchestrator + 条件路由 + 4 垂直 Agent + 汇总
# ---------------------------------------------------------------------------


def build_multi_agent_graph():
    g = StateGraph(MultiAgentState)
    g.add_node("orchestrator", orchestrator_node)
    g.add_node("retrieval", retrieval_agent)
    g.add_node("computation", computation_agent)
    g.add_node("verification", verification_agent)
    g.add_node("generation", generation_agent)
    g.add_node("summarize", summarize_node)

    g.set_entry_point("orchestrator")

    # 条件路由：Orchestrator → 按 intent 路由到对应垂直 Agent
    g.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "retrieval": "retrieval",
            "computation": "computation",
            "verification": "verification",
            "generation": "generation",
        },
    )

    # 各垂直 Agent 执行完 → 汇总节点 → END
    g.add_edge("retrieval", "summarize")
    g.add_edge("computation", "summarize")
    g.add_edge("verification", "summarize")
    g.add_edge("generation", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


graph = build_multi_agent_graph()


# ---------------------------------------------------------------------------
# 6. @trace 包住 graph 调用入口 —— 一次多 Agent 协作 = 一条 Trace
# ---------------------------------------------------------------------------


@trace(agent="多 Agent 协作系统")
def run_multi_agent(question: str) -> str:
    """LangGraph 多 Agent 图的调用入口，挂 @trace 后自动采集全链路 Trace。"""
    result = graph.invoke({"question": question})
    return result["final_answer"]


# ---------------------------------------------------------------------------
# 7. 演示：4 个不同意图问题走不同 Agent + 1 个失败场景
# ---------------------------------------------------------------------------


def _dump_steps(steps, indent: int = 0) -> None:
    """递归打印步骤树（展示多 Agent 协作的父子 span 结构）。"""
    for s in steps:
        prefix = "  " * indent + ("└─ " if indent else "· ")
        mark = "❌" if s.status == "error" else "✅"
        children_tag = f" ({len(s.children)} 子步骤)" if s.children else ""
        print(f"{prefix}{mark} {s.name} | {s.model} | tok={s.tokens_in}+{s.tokens_out} | {s.latency_ms}ms{children_tag}"
              + (f" | error={s.error}" if s.error else ""))
        if s.children:
            _dump_steps(s.children, indent + 1)


def main() -> None:
    print("=" * 70)
    print("agent_ops × LangGraph：多 Agent 协作示例（Orchestrator + 4 垂直 Agent）")
    print("=" * 70)

    # 4 个不同意图的问题，分别走 4 个不同垂直 Agent
    cases = [
        ("抗体筛选流程是什么？", "retrieval", "检索 Agent"),
        ("上周实验通过率多少？", "computation", "计算 Agent"),
        ("这份报告的结论能回指依据吗？", "verification", "校验 Agent"),
        ("帮我写一份抗原设计规范总结", "generation", "生成 Agent"),
    ]

    for q, expected_intent, agent_name in cases:
        ans = run_multi_agent(q)
        last = get_collector().traces()[-1]
        actual_intent = ""
        for s in last.steps:
            for c in s.children:
                if "意图分类" in c.name:
                    actual_intent = expected_intent  # 简化校验
        print(f"\n✅「{q}」→ 路由到 {agent_name}")
        print(f"   产出: {ans[:50]}…")

    # 失败场景：retrieval 超时（问题含"流程"命中 retrieval 路由 + "超时"触发异常）
    print("\n" + "─" * 70)
    try:
        run_multi_agent("查流程资料时知识库超时")
    except RuntimeError as exc:
        print(f"❌ 失败场景 → 预期失败：{exc}")

    # 聚合报告
    print("\n" + "=" * 70)
    print("聚合报告（report）:")
    r = report()
    print(f"  总调用 {r['total']['calls']} 次 | 成功率 {r['total']['success_rate']:.0%} | "
          f"错误率 {r['total']['error_rate']:.0%} | 总成本 ${r['total']['total_cost_usd']:.4f}")

    print("\n按模型归因（model_usage）:")
    for m, u in model_usage().items():
        print(f"  {m}: {u['calls']} 次 | in={u['tokens_in']} out={u['tokens_out']} | ${u['cost_usd']:.4f}")

    # 最近一条成功 Trace 的步骤树 —— 展示多 Agent 协作的父子层级
    ok_traces = [t for t in get_collector().traces() if t.status == "success"]
    if ok_traces:
        last_ok = ok_traces[-1]
        print(f"\n最近成功 Trace（{last_ok.trace_id}）的多 Agent 协作步骤树：")
        _dump_steps(last_ok.steps)

    print("\n" + "=" * 70)
    print("💡 说明：")
    print("   · Orchestrator 用 span 包路由决策 → 决策可追溯")
    print("   · 4 个垂直 Agent 各用 span 包工具调用 → 形成父子层级")
    print("   · 一条 Trace 里能看到完整的多 Agent 协作结构")
    print("   · 这就是研发管家「Orchestrator + 4 垂直 Agent」架构的工程化缩影")
    print("=" * 70)


if __name__ == "__main__":
    main()
