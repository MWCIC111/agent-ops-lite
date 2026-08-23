"""quickstart.py — 3 行接入示例

演示 agent_ops 核心库的完整流程：采集 → 聚合 → 报告。
直接运行：
    python examples/quickstart.py

面试叙事："接入任意 Agent 只需要 @trace 装饰器，函数正常写，
采集、聚合、成本核算全部自动完成——数据结构与观测面板完全一致。"
"""
from __future__ import annotations

import os
import random
import sys

# 开发模式：让 `python examples/quickstart.py` 直接可运行（把项目根目录加入路径）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_ops import record_step, report, trace

# ---------------------------------------------------------------------------
# ① 接入：给任意 Agent 函数挂上 @trace 装饰器
# ---------------------------------------------------------------------------


@trace(agent="检索 Agent", model="qwen-max")
def retrieval_agent(question: str) -> str:
    """检索 Agent：意图识别 → 知识库检索 → 内容生成"""
    record_step("意图识别", model="qwen-plus", tokens_in=300, tokens_out=80,
                latency_ms=350)
    record_step("知识检索", model="qwen-plus", tool="知识库检索",
                tokens_in=800, tokens_out=120, latency_ms=1250)
    record_step("内容生成", model="qwen-max",
                tokens_in=500, tokens_out=300, latency_ms=900)
    if "失败" in question:
        raise RuntimeError("知识库检索超时")
    return f"【检索结果】关于「{question}」的参考资料……"


@trace(agent="推理 Agent", model="gpt-4o")
def reasoning_agent(question: str) -> str:
    """推理 Agent：任务规划 → 代码执行 → 结论"""
    record_step("任务规划", model="gpt-4o", tokens_in=400, tokens_out=150,
                latency_ms=420)
    record_step("代码执行", model="gpt-4o", tool="代码执行",
                tokens_in=200, tokens_out=100, latency_ms=1800)
    return f"【推理结论】{question} 的分析结果……"


# ---------------------------------------------------------------------------
# ② 正常调用业务函数（采集全部自动完成）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    questions = [
        "AgentOps 是什么",
        "如何做灰度发布",
        "RAG 检索效果怎么评估",
    ]
    for q in questions:
        print(f"调用检索 Agent: {q}")
        print("  ", retrieval_agent(q))

    # 演示失败自动标记：该调用会抛异常，但 Trace 会被标记为 failed
    try:
        retrieval_agent("测试失败场景")
    except RuntimeError as e:
        print(f"调用失败（已自动记录）: {e}")

    print()
    for q in ["成本怎么优化", "延迟怎么降"]:
        print(f"调用推理 Agent: {q}")
        print("  ", reasoning_agent(q))

    # ------------------------------------------------------------------
    # ③ 一键出报告：成功率 / 延迟 / token / 成本（面板同口径）
    # ------------------------------------------------------------------
    print("\n" + "=" * 46)
    r = report()
    print(f"统计窗口：{r['window']}")
    t = r["total"]
    print(f"总调用    : {t['calls']} 次")
    print(f"成功率    : {t['success_rate']:.1%}")
    print(f"平均延迟  : {t['avg_latency_ms']:.0f} ms")
    print(f"总 Token  : {t['total_tokens']:,}")
    print(f"总成本    : ${t['total_cost_usd']:.4f}（≈¥{t['total_cost_usd'] * 7.2:.3f}）")
    print("-" * 46)
    for agent, m in r["by_agent"].items():
        print(f"{agent:<8} 调用 {m['calls']} 次 | 成功率 {m['success_rate']:.0%} | "
              f"平均延迟 {m['avg_latency_ms']:.0f}ms | 成本 ${m['total_cost_usd']:.4f}")
    print("=" * 46)
