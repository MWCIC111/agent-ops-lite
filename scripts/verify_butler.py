"""本地验证：mock DeepSeek，确认研发管家多步编排产生 7 步 Trace（不依赖 API Key）。"""
import sys, types
from unittest.mock import MagicMock

# 用 MagicMock 替换 streamlit，避免模块加载时 st.* 调用报错
_st = MagicMock()
_st.columns.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
sys.modules["streamlit"] = _st

import importlib.util
import os

REPO = "D:/repos/agent-ops-lite"
sys.path.insert(0, os.path.join(REPO, "app"))
spec = importlib.util.spec_from_file_location(
    "real_agent_mod", os.path.join(REPO, "app/pages/8_真实Agent.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# mock 掉 DeepSeek 调用，返回 (文本, in, out)
def fake_chat(model, messages, temperature=0.3):
    return ("MOCK_ANSWER", 12, 6)

mod._deepseek_chat = fake_chat

# 运行研发管家场景
ans = mod.run_real_agent("研发管家 · 研发问答", "如何设计多 Agent 的共享状态？", "deepseek-chat")
traces = mod.SQLiteStore(mod.DB_PATH).load()
last = traces[-1]
step_names = [s.name for s in last.steps]
print("ANSWER_HEAD:", ans[:40].replace("\n", " "))
print("TRACE_AGENT:", last.agent)
print("STEP_COUNT:", len(step_names))
for i, n in enumerate(step_names, 1):
    print(f"  {i}. {n}")
expected = [
    "共享State · 知识检索",
    "Orchestrator · 任务编排",
    "抗原设计 Agent",
    "方案规划 Agent",
    "故障诊断 Agent",
    "资料整理 Agent",
    "置信度融合 · 三层幻觉抑制",
]
ok = step_names == expected
print("MATCH_EXPECTED:", ok)
assert ok, "step 顺序与数量不符合预期"
print("DRYRUN_OK")
