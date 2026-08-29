"""demo_data.py — 生成模拟 Agent 调用 Trace 数据

设计要点（面试时可直接讲）：
1. 数据结构复用 agent_ops 核心库（单一事实来源），与真实采集的 Trace 完全一致
   → Demo 不是"画假面板"，而是核心库的真实演示；换真实数据源页面零改动
2. 固定随机种子 (seed=42)，每次运行数据可复现，方便截图/录屏
3. 包含失败与重试场景，方便演示告警与异常页
4. 末尾合并真实落库 Trace（来自真实 LLM API 调用），让模拟/真实在数据层面统一
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta

# 让本模块能 import 到仓库根的 agent_ops（Streamlit 多页面下 app/ 非仓库根）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 复用核心库的数据结构与单价表（单一事实来源，消除与 app/pages 的双定义漂移）
from agent_ops import MODEL_PRICE, Step, Trace  # noqa: E402
from agent_ops.storage import SQLiteStore  # noqa: E402

random.seed(42)

# ---------------- 基础配置 ----------------

# 4 个垂直 Agent（与 7_Agent拓扑.py 的节点命名完全一致，保证跨页联动自洽）
AGENTS = ["规划 Agent", "检索 Agent", "推理 Agent", "校验 Agent"]

TOOLS = ["网页搜索", "数据库查询", "接口调用", "代码执行", "知识库检索"]

# Agent 执行链的步骤模板：(步骤名, 模型, 调用的工具)
STEP_TEMPLATES = [
    ("意图识别", "qwen-plus", None),
    ("任务规划", "gpt-4o", None),
    ("知识检索", "qwen-plus", "知识库检索"),
    ("工具调用", "gpt-4o", "接口调用"),
    ("数据查询", "qwen-plus", "数据库查询"),
    ("代码执行", "gpt-4o", "代码执行"),
    ("内容生成", "qwen-max", None),
]


# ---------------- 生成器 ----------------

def _gen_step(template: tuple[str, str, str | None], fail: bool) -> Step:
    name, model, tool = template
    tok_in = random.randint(200, 1200)
    tok_out = random.randint(100, 800)
    latency = random.randint(150, 3000)
    if fail:
        return Step(name, model, tool, tok_in, tok_out, latency, "error",
                    error=random.choice(["工具超时", "响应格式错误", "限流触发"]))
    return Step(name, model, tool, tok_in, tok_out, latency, "success")


def _gen_trace(at: datetime) -> Trace:
    trace_id = "".join(random.choices("0123456789abcdef", k=8))
    agent = random.choice(AGENTS)
    n_steps = random.randint(3, 8)
    steps: list[Step] = []
    failed = False
    for i in range(n_steps):
        template = random.choice(STEP_TEMPLATES)
        # 每步约 6% 概率失败；失败后重试一次（模拟生产的重试机制）
        fail = random.random() < 0.06 and not failed
        steps.append(_gen_step(template, fail))
        if fail:
            failed = True
            steps.append(_gen_step(template, False))
    status = "failed" if failed and random.random() < 0.5 else "success"
    trace = Trace(trace_id, agent, at, steps, status)
    trace.summarize()  # 聚合统计（复用核心库逻辑，未知模型按 0 价，安全）
    return trace


def load_real_traces() -> list[Trace]:
    """读取真实落库的 Trace（来自真实 LLM API 调用），供全面板消费。"""
    try:
        real_store = SQLiteStore(os.path.join(_REPO_ROOT, "agent_ops.db"))
        return real_store.load()
    except Exception:
        return []


def load_demo_traces(days: int = 14, n: int = 2000) -> list[Trace]:
    """生成最近 n 天的模拟 Trace（按时间倒序），并合并真实落库 Trace。

    注：新版统一数据源为 load_traces()（真实优先）。本函数保留作兜底/兼容。
    """
    now = datetime.now()
    traces = []
    for _ in range(n):
        at = now - timedelta(
            minutes=random.randint(0, days * 24 * 60),
            seconds=random.randint(0, 59),
        )
        traces.append(_gen_trace(at))
    traces.sort(key=lambda t: t.started_at, reverse=True)
    # 追加真实落库的 Trace（来自真实 LLM API 调用），不改动模拟基线
    try:
        traces.extend(load_real_traces())
        traces.sort(key=lambda t: t.started_at, reverse=True)
    except Exception:
        pass
    return traces


def load_traces() -> tuple[list[Trace], str]:
    """统一数据源（真实优先）。

    返回 (traces, mode)：
      - mode == "real"：全部来自 agent_ops.db 的真实 LLM 调用落库 Trace
        （真实 token / 延迟 / 成本 / 工具 / 知识库召回），全面板直接消费。
      - mode == "mock"：数据库为空时的可复现模拟兜底（避免面板空白），
        并显式标记，便于页面打「模拟数据」标识。

    设计意图（面试可直接讲）：Demo 不是「画假面板」，换上真实数据源后
    所有观测页面零改动——这就是 AgentOps 生产级可观测性。
    """
    real = load_real_traces()
    if real:
        return real, "real"
    # 兜底：数据库为空，生成可复现模拟数据
    now = datetime.now()
    traces = []
    for _ in range(2000):
        at = now - timedelta(
            minutes=random.randint(0, 14 * 24 * 60),
            seconds=random.randint(0, 59),
        )
        traces.append(_gen_trace(at))
    traces.sort(key=lambda t: t.started_at, reverse=True)
    return traces, "mock"


def real_baseline(traces: list[Trace]) -> dict:
    """从真实 Trace 聚合生产基线指标，供版本对比 / 灰度发布页做真实基线。"""
    if not traces:
        return {}
    n = len(traces)
    succ = sum(1 for t in traces if t.status == "success") / n
    lat = sum(t.latency_ms for t in traces) / n / 1000.0
    tok = sum(t.tokens for t in traces) / n
    tool_steps = [s.status for t in traces for s in t.steps if s.tool]
    tool_succ = (
        sum(1 for stt in tool_steps if stt == "success") / len(tool_steps)
        if tool_steps else 0.0
    )
    return {
        "success_rate": succ,
        "avg_latency_s": lat,
        "avg_tokens": tok,
        "tool_success_rate": tool_succ,
    }


if __name__ == "__main__":
    ts = load_demo_traces()
    print(f"生成 {len(ts)} 条 Trace（含真实落库）")
    print(f"首条: {ts[0].trace_id} | {ts[0].agent} | {ts[0].status} | "
          f"{ts[0].tokens} tokens | {ts[0].latency_ms}ms | ${ts[0].cost_usd:.4f}")
    print(f"成功率: {sum(1 for t in ts if t.status == 'success') / len(ts):.1%}")
