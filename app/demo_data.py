"""demo_data.py — 生成模拟 Agent 调用 Trace 数据

设计要点（面试时可直接讲）：
1. 数据结构与生产环境真实采集的 Trace 完全一致
   → Demo 不是"画假面板"，而是核心库的真实演示；换真实数据源页面零改动
2. 固定随机种子 (seed=42)，每次运行数据可复现，方便截图/录屏
3. 包含失败与重试场景，方便演示告警与异常页
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

random.seed(42)

# ---------------- 基础配置 ----------------

# 4 个垂直 Agent（与 7_Agent拓扑.py 的节点命名完全一致，保证跨页联动自洽）
AGENTS = ["规划 Agent", "检索 Agent", "推理 Agent", "校验 Agent"]

# 每 1K token 价格（美元）：(input, output)
MODEL_PRICE = {
    "gpt-4o": (0.0025, 0.0100),
    "qwen-max": (0.0015, 0.0060),
    "qwen-plus": (0.0004, 0.0012),
}

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

# ---------------- 数据结构 ----------------

@dataclass
class Step:
    """Agent 执行链中的一步（可嵌套子步骤，形成父子 span 树）"""
    name: str
    model: str
    tool: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: int
    status: str          # success | error
    error: str | None = None
    children: list["Step"] = field(default_factory=list)  # 子步骤（父子 span）


@dataclass
class Trace:
    """一次完整的 Agent 调用链路"""
    trace_id: str
    agent: str
    started_at: datetime
    steps: list[Step]
    status: str          # success | failed
    tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0

    def summarize(self) -> None:
        """聚合统计：总 token、总延迟、成本（按模型单价折算）"""
        self.tokens = sum(s.tokens_in + s.tokens_out for s in self.steps)
        self.latency_ms = sum(s.latency_ms for s in self.steps)
        self.cost_usd = sum(
            (s.tokens_in * MODEL_PRICE[s.model][0] + s.tokens_out * MODEL_PRICE[s.model][1]) / 1000
            for s in self.steps
        )


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
    trace.summarize()
    return trace


def load_demo_traces(days: int = 14, n: int = 2000) -> list[Trace]:
    """生成最近 n 天的模拟 Trace（按时间倒序）"""
    now = datetime.now()
    traces = []
    for _ in range(n):
        at = now - timedelta(
            minutes=random.randint(0, days * 24 * 60),
            seconds=random.randint(0, 59),
        )
        traces.append(_gen_trace(at))
    traces.sort(key=lambda t: t.started_at, reverse=True)
    return traces


if __name__ == "__main__":
    ts = load_demo_traces()
    print(f"生成 {len(ts)} 条 Trace")
    print(f"首条: {ts[0].trace_id} | {ts[0].agent} | {ts[0].status} | "
          f"{ts[0].tokens} tokens | {ts[0].latency_ms}ms | ${ts[0].cost_usd:.4f}")
    print(f"成功率: {sum(1 for t in ts if t.status == 'success') / len(ts):.1%}")
