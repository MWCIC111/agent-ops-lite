"""本地验证：mock 统一 LLM 入口，确认研发管家多步编排产生 7 步 Trace（不依赖 API Key）。

低置信时会追加第 8 步「人工审核回写 · 已转人工队列」；本脚本 mock 的检索命中会
走高置信路径，基础 7 步保持确定性，同时允许可选的第 8 步存在。
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
    return ("MOCK_CONTEXT", [{
        "title": "mock", "content": "mock", "source": "wiki", "score": 1.0,
    }])


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
