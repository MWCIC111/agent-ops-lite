"""examples/langgraph_example.py — 核心库 × LangGraph 真实接入示例

用 LangGraph 的 StateGraph 搭建一个 3 节点的「研发问答 Agent」：
    检索节点 → 生成节点 → 校验节点

@trace 装饰器包住 graph 调用入口，节点函数内用 record_step 记录每一步——
证明 agent_ops 不绑定任何框架，LangGraph / LangChain / 自研框架都能接入。

运行：
    python examples/langgraph_example.py
"""
from __future__ import annotations

import os
import sys

# 开发模式：允许从项目根目录直接运行（python examples/langgraph_example.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import time
from typing import TypedDict

from langgraph.graph import END, StateGraph

from agent_ops import record_step, report, trace


# ---------------------------------------------------------------------------
# 1. 定义 LangGraph State —— 一次调用的共享状态
# ---------------------------------------------------------------------------


class QAState(TypedDict):
    question: str
    context: str
    answer: str
    verified: bool


# ---------------------------------------------------------------------------
# 2. 三个节点：检索 / 生成 / 校验
#    每个节点 = Agent 执行链上的一步，用 record_step 记录（模型/工具/token/耗时）
# ---------------------------------------------------------------------------


def retrieve_node(state: QAState) -> dict:
    """检索节点：从知识库取依据（模拟）。"""
    time.sleep(random.uniform(0.2, 0.5))
    if "超时" in state["question"]:
        # 模拟工具调用超时 → 抛异常，@trace 自动把整条 Trace 标记为 failed
        raise RuntimeError("知识库检索超时（ToolTimeout）")
    context = f"【依据】关于「{state['question']}」的研发资料片段……"
    record_step(
        "知识库检索",
        model="qwen-plus",
        tool="知识库检索",
        tokens_in=400,
        tokens_out=120,
        latency_ms=random.randint(200, 500),
    )
    return {"context": context}


def generate_node(state: QAState) -> dict:
    """生成节点：基于依据生成回答（模拟 LLM 调用）。"""
    time.sleep(random.uniform(0.3, 0.8))
    answer = f"【回答】基于检索依据，{state['question']} 的结论是……"
    record_step(
        "内容生成",
        model="qwen-max",
        tokens_in=520,
        tokens_out=300,
        latency_ms=random.randint(300, 800),
    )
    return {"answer": answer}


def verify_node(state: QAState) -> dict:
    """校验节点：结论必须回指依据，回指不上视为失败（模拟医疗级校验）。"""
    time.sleep(random.uniform(0.1, 0.3))
    verified = "【依据】" in state.get("context", "")
    record_step(
        "结果校验",
        model="qwen-plus",
        tokens_in=200,
        tokens_out=50,
        latency_ms=random.randint(100, 300),
    )
    if not verified:
        # 抛异常 → @trace 自动把整条 Trace 标记为 failed
        raise RuntimeError("校验失败：结论无法回指检索依据")
    return {"verified": True}


# ---------------------------------------------------------------------------
# 3. 组装 LangGraph 图：检索 → 生成 → 校验
# ---------------------------------------------------------------------------


def build_graph():
    g = StateGraph(QAState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("generate", generate_node)
    g.add_node("verify", verify_node)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", "verify")
    g.add_edge("verify", END)
    return g.compile()


graph = build_graph()


# ---------------------------------------------------------------------------
# 4. 用 @trace 包住 graph 调用入口 —— 一次完整调用 = 一条 Trace
# ---------------------------------------------------------------------------


@trace(agent="研发问答 Agent")
def run_agent(question: str) -> str:
    """LangGraph 图的调用入口，挂上 @trace 后自动采集全链路 Trace。"""
    result = graph.invoke({"question": question})
    return result["answer"]


# ---------------------------------------------------------------------------
# 5. 演示：正常调用 + 失败调用 → report() 一键出指标
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 62)
    print("agent_ops × LangGraph：真实框架接入示例")
    print("=" * 62)

    # 正常调用 × 2
    for q in ["抗体筛选流程", "抗原设计规范"]:
        ans = run_agent(q)
        print(f"\n✅ 提问「{q}」→ {ans[:36]}…")

    # 失败调用 × 1（模拟检索环节异常）
    try:
        run_agent("异常场景：知识库超时")
    except RuntimeError as exc:
        print(f"\n❌ 提问「异常场景」→ 预期失败：{exc}")

    # 聚合报告
    print("\n" + "=" * 62)
    print(report())
    print("=" * 62)
    print("\n💡 说明：3 次调用中 1 次失败，report() 自动统计成功率与失败原因。")
    print("   生产环境把 report() 换成写 ES / 面板数据源，8 个观测页面即可直接消费。")


if __name__ == "__main__":
    main()
