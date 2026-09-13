"""本地验证：mock 统一 LLM 入口，确认研发管家多步编排产生 7 步 Trace（不依赖 API Key）。

步骤序列 = 基础 7 步（共享State检索 / Orchestrator编排 / 4 个垂直 Agent / 置信度融合），
低置信时追加第 8 步「人工审核回写 · 已转人工队列」（可选尾步）。

检索 mock 说明（2026-09-13 二次校订）：线上 retrieve(top_k) 返回 top_k 条命中，检索层
硬门控为「两个信号都报警才强制低置信」——
    top1 < ABS_GATE(25.4)  且  ratio = top1/mean(top_k) < RATIO_GATE(1.14)
本脚本的 mock 按**真实语料量级**给分（top1=40.0 ≥ 25.4，ratio≈1.39 ≥ 1.14），两个信号
都不报警 → 走高置信路径，基础 7 步确定性成立。若 mock 分数只有个位数（早期版本是 3.0），
top1 会低于 ABS_GATE 而按设计转人工，尾步变成第 8 步——那是量级不真实造成的假失败。
"""
import os
import sys
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_runner  # noqa: E402
from agent_ops import MemoryStore  # noqa: E402


class _FakeChoice:
    def __init__(self, text: str = "MOCK_ANSWER"):
        self.message = SimpleNamespace(content=text)
        self.logprobs = None


class _FakeResponse:
    def __init__(self, text: str = "MOCK_ANSWER", tin: int = 12, tout: int = 6):
        self.choices = [_FakeChoice(text)]
        self.usage = SimpleNamespace(prompt_tokens=tin, completion_tokens=tout)


def fake_completion(model, messages, temperature=0.3, max_tokens=500, **extra):
    """替换 agent_runner._chat_completion，覆盖普通与 logprobs 两种入口。"""
    return _FakeResponse()


def fake_retrieve(question, top_k=3):
    """模拟线上 retrieve：返回 top_k 条分数递减的命中（而非单条）。

    分数按**真实 IVD 语料的 BM25 量级**给：语料上库内查询的 top1 实测中位 55、
    负例上限约 29，噪声底 p90 约 26。这里取 top1=40（明显高于 ABS_GATE=25.4）
    且 ratio≈1.39（高于 RATIO_GATE=1.14），代表「检索支撑充分」的高置信路径。
    """
    k = max(int(top_k), 1)
    hits = [
        {"title": "mock", "content": "mock", "source": "wiki", "score": 40.0 - 8.0 * i}
        for i in range(k)
    ]
    return ("MOCK_CONTEXT", hits)


agent_runner._chat_completion = fake_completion
agent_runner._retrieve_context = fake_retrieve
# 用内存存储隔离验证，避免往开发库 agent_ops.db 写测试 Trace
agent_runner.collector._storage = MemoryStore()

ans = agent_runner.run_real_agent(
    "研发管家 · 研发问答", "如何设计多 Agent 的共享状态？", "deepseek-chat"
)
traces = agent_runner.collector.traces()
last = traces[-1]
step_names = [s.name for s in last.steps]
print("ANSWER_HEAD:", ans[:40].replace("\n", " "))
print("TRACE_AGENT:", last.agent)
print("STEP_COUNT:", len(step_names))
for i, n in enumerate(step_names, 1):
    print(f"  {i}. {n}")

required = [
    "共享State · 知识检索",
    "Orchestrator · 任务编排",
    "抗原设计 Agent",
    "方案规划 Agent",
    "故障诊断 Agent",
    "资料整理 Agent",
    "置信度融合 · 三层幻觉抑制",
]
optional_tail = ["人工审核回写 · 已转人工队列"]
ok = step_names[: len(required)] == required
ok = ok and len(step_names) in (len(required), len(required) + 1)
if len(step_names) == len(required) + 1:
    ok = ok and step_names[len(required):] == optional_tail
print("MATCH_EXPECTED:", ok)
assert ok, "step 顺序与数量不符合预期"
print("DRYRUN_OK")
