"""agent_ops — Agent 可观测与成本管控核心库（零依赖）

3 行接入任意 Agent：
    from agent_ops import trace, record_step, report

    @trace(agent="检索 Agent")
    def my_agent(question: str) -> str:
        record_step("知识检索", model="qwen-plus", tool="知识库检索",
                    tokens_in=500, tokens_out=200)
        return "答案"

    my_agent("什么是 AgentOps?")
    print(report())

采集的 Trace 数据结构与 app/demo_data.py 完全一致 —— 面板可直接消费。
"""
from .alerts import AlertEvent, AlertRule, WebhookAlert, send_alert
from .cost import MODEL_PRICE, USD_TO_CNY, step_cost_cny, step_cost_usd
from .metrics import model_usage, report, traces_to_rows
from .storage import MemoryStore, SQLiteStore, TraceStore, dict_to_trace, trace_to_dict
from .tracer import (
    Collector,
    Step,
    Trace,
    get_collector,
    record_step,
    span,
    trace,
)

__version__ = "0.3.0"

__all__ = [
    "AlertEvent",
    "AlertRule",
    "Collector",
    "MemoryStore",
    "SQLiteStore",
    "Step",
    "Trace",
    "TraceStore",
    "MODEL_PRICE",
    "USD_TO_CNY",
    "WebhookAlert",
    "dict_to_trace",
    "get_collector",
    "model_usage",
    "record_step",
    "report",
    "send_alert",
    "span",
    "step_cost_cny",
    "step_cost_usd",
    "trace",
    "trace_to_dict",
    "traces_to_rows",
    "__version__",
]
