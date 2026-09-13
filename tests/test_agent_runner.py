"""test_agent_runner.py — 真实 Agent 韧性层回归（需要 app/requirements.txt 依赖）

覆盖 _chat_completion 的退避重试与兜底模型契约：
  1. 主模型耗尽重试后切 DEEPSEEK_FALLBACK_MODEL 并成功返回
  2. 主/兜底模型都持续瞬态失败时，重试耗尽后抛出最后一条错误
  3. 未配置兜底模型时只重试主模型

运行：
    python tests/test_agent_runner.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["AGENTOPS_DAILY_QUOTA_CNY"] = "0"
os.environ["AGENTOPS_BACKOFF_S"] = "0.01"

# Windows 默认 GBK 控制台无法打印 ✅/❌：统一强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import agent_runner  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


class _TransientError(RuntimeError):
    pass


class _FakeResponse:
    def __init__(self, text: str = "ok"):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=text))]
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)


_CALLS: list[str] = []
_FAIL_MODELS: set[str] = set()


class _FakeCompletions:
    def create(self, **kwargs):
        model = kwargs["model"]
        _CALLS.append(model)
        if model in _FAIL_MODELS:
            raise _TransientError("transient failure")
        return _FakeResponse()


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.chat = _FakeChat()


def _reset(fail_models: set[str], fallback: str | None) -> None:
    _CALLS.clear()
    _FAIL_MODELS.clear()
    _FAIL_MODELS.update(fail_models)
    agent_runner._FALLBACK_MODEL = fallback
    agent_runner._MAX_RETRIES = 3
    agent_runner._BASE_BACKOFF = 0.0
    agent_runner._RETRYABLE = (_TransientError,)
    agent_runner.openai.OpenAI = _FakeOpenAI


def main() -> None:
    print("== 韧性层：_chat_completion 兜底模型 ==")

    # 1. 主模型 3 次耗尽后切兜底并成功
    _reset(fail_models={"main"}, fallback="fallback")
    resp = agent_runner._chat_completion("main", [{"role": "user", "content": "hi"}])
    check("主模型耗尽后切兜底成功", resp.choices[0].message.content == "ok")
    check("调用顺序为主模型 x3 + 兜底 x1",
          _CALLS == ["main", "main", "main", "fallback"], f"got {_CALLS}")

    # 2. 主/兜底都失败：耗尽后抛最后一条瞬态错误
    _reset(fail_models={"main", "fallback"}, fallback="fallback")
    try:
        agent_runner._chat_completion("main", [{"role": "user", "content": "hi"}])
        check("全部模型失败时抛出异常", False)
    except _TransientError:
        check("全部模型失败时抛出异常", True)
        check("主/兜底各重试 3 次",
              _CALLS == ["main", "main", "main", "fallback", "fallback", "fallback"],
              f"got {_CALLS}")

    # 3. 未配置兜底：只重试主模型
    _reset(fail_models={"main"}, fallback=None)
    try:
        agent_runner._chat_completion("main", [{"role": "user", "content": "hi"}])
        check("无兜底时主模型耗尽抛异常", False)
    except _TransientError:
        check("无兜底时主模型耗尽抛异常", True)
        check("无兜底时只调用主模型",
              _CALLS == ["main", "main", "main"], f"got {_CALLS}")

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
