"""tracer.py — Trace 采集核心

@trace 装饰器包裹任意 Agent 函数，自动完成：
  1. 生成 trace_id、记录开始时间
  2. 函数内通过 record_step() 记录每一步（模型 / 工具 / token / 耗时）
  3. 函数抛异常 → Trace 自动标记 failed
  4. 结束时自动聚合：总 token / 总延迟 / 成本（与面板口径一致）

零依赖：仅使用 Python 标准库。
"""
from __future__ import annotations

import functools
import random
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

from .cost import MODEL_PRICE

# ---------------------------------------------------------------------------
# 数据结构 —— 与 app/demo_data.py 的 Step / Trace 字段完全一致
# （保证核心库采集的数据可以直接喂给现有 8 个观测页面，零改动）
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """Agent 执行链中的一步"""
    name: str
    model: str
    tool: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: int
    status: str          # success | error
    error: str | None = None


@dataclass
class Trace:
    """一次完整的 Agent 调用链路"""
    trace_id: str
    agent: str
    started_at: datetime
    steps: list[Step] = field(default_factory=list)
    status: str = "success"   # success | failed
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


# ---------------------------------------------------------------------------
# Collector —— 内存采集器（生产可替换为 ES / ClickHouse / 对象存储）
# ---------------------------------------------------------------------------


class Collector:
    """线程安全的 Trace 采集器。生产环境可将 add() 替换为写持久化存储。"""

    def __init__(self) -> None:
        self._traces: list[Trace] = []
        self._lock = threading.Lock()

    def add(self, trace: Trace) -> None:
        with self._lock:
            self._traces.append(trace)

    def traces(self) -> list[Trace]:
        with self._lock:
            return list(self._traces)

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


_collector = Collector()


def get_collector() -> Collector:
    """获取全局采集器（可替换为自定义实例）"""
    return _collector


# ---------------------------------------------------------------------------
# 当前 Trace 上下文 —— 支持嵌套调用与并发（contextvars 线程安全）
# ---------------------------------------------------------------------------


@dataclass
class _TraceContext:
    trace: Trace
    start_mark: float
    last_mark: float


_current: ContextVar[_TraceContext | None] = ContextVar("agent_ops_current", default=None)


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def _random_latency() -> int:
    """模拟一步调用的耗时（150ms ~ 3000ms）。生产环境由真实调用计时替代。"""
    return random.randint(150, 3000)


def record_step(
    name: str,
    model: str | None = None,
    tool: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    status: str = "success",
    error: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """在当前 @trace 装饰的函数内记录一步。

    latency_ms 缺省时按"上一步到这一步"的间隔自动计时。
    """
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("record_step() 必须在 @trace 装饰的函数内调用")
    if latency_ms is None:
        now = time.monotonic()
        latency_ms = max(int((now - ctx.last_mark) * 1000), 1)
        ctx.last_mark = now
    step = Step(
        name=name,
        model=model or "qwen-plus",
        tool=tool,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        status=status,
        error=error,
    )
    ctx.trace.steps.append(step)


def trace(agent: str = "Agent", model: str | None = None, collector: Collector | None = None):
    """装饰器：包裹任意 Agent 函数，自动采集 Trace。

    >>> @trace(agent="检索 Agent")
    ... def my_agent(question: str) -> str:
    ...     record_step("知识检索", model="qwen-plus", tool="知识库检索",
    ...                 tokens_in=500, tokens_out=200)
    ...     return "答案"
    """
    sink = collector or _collector

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started_at = datetime.now()
            start_mark = time.monotonic()
            tr = Trace(trace_id=_new_trace_id(), agent=agent, started_at=started_at)
            ctx = _TraceContext(trace=tr, start_mark=start_mark, last_mark=start_mark)
            token = _current.set(ctx)
            try:
                result = func(*args, **kwargs)
                tr.status = "success"
            except Exception as exc:  # noqa: BLE001 —— 任何异常都记为失败
                tr.status = "failed"
                # 把异常作为一步失败记录，便于面板展示失败原因
                record_step(
                    name="异常中断",
                    model=model,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            finally:
                _current.reset(token)
                tr.latency_ms = max(int((time.monotonic() - start_mark) * 1000), 1)
                tr.summarize()
                sink.add(tr)
            return result

        return wrapper

    return decorator
