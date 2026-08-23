"""metrics.py — 指标聚合

把 Collector 里的 Trace 聚合成可读指标，输出结构与面板 KPI 口径一致：
总调用量 / 成功率 / 平均延迟 / 总 token / 总成本，并按 Agent 分组。
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from .tracer import Collector, Trace, get_collector


def _agg_traces(traces: list[Trace]) -> dict[str, Any]:
    """聚合一组 Trace 的统计指标"""
    n = len(traces)
    if n == 0:
        return {
            "calls": 0, "success_rate": 0.0, "avg_latency_ms": 0.0,
            "total_tokens": 0, "total_cost_usd": 0.0,
        }
    ok = sum(1 for t in traces if t.status == "success")
    return {
        "calls": n,
        "success_rate": ok / n,
        "avg_latency_ms": statistics.mean(t.latency_ms for t in traces),
        "total_tokens": sum(t.tokens for t in traces),
        "total_cost_usd": sum(t.cost_usd for t in traces),
    }


def report(collector: Collector | None = None, since: datetime | None = None) -> dict[str, Any]:
    """生成聚合报告。

    collector 缺省用全局采集器；since 传入时只统计该时间点之后的 Trace。
    返回结构：
        {
          "total": {...总体指标...},
          "by_agent": {agent名: {...该Agent指标...}},
          "window": "..."   # 统计窗口描述
        }
    """
    sink = collector or get_collector()
    traces = sink.traces()
    if since is not None:
        traces = [t for t in traces if t.started_at >= since]

    total = _agg_traces(traces)

    by_agent: dict[str, dict[str, Any]] = {}
    for agent in sorted({t.agent for t in traces}):
        agent_traces = [t for t in traces if t.agent == agent]
        by_agent[agent] = _agg_traces(agent_traces)

    window = f"近 {len(traces)} 次调用"
    if since is not None:
        window = f"自 {since:%Y-%m-%d %H:%M} 起 {len(traces)} 次调用"

    return {"total": total, "by_agent": by_agent, "window": window}


def traces_to_rows(traces: list[Trace] | None = None, collector: Collector | None = None) -> list[dict]:
    """把 Trace 展平成行（与面板 1_链路追踪.py 的表格字段一致）。

    生产环境接入时，面板直接消费这份数据即可。
    """
    sink = collector or get_collector()
    rows = []
    for t in (traces if traces is not None else sink.traces()):
        rows.append({
            "trace_id": t.trace_id,
            "agent": t.agent,
            "time": t.started_at,
            "status": t.status,
            "n_steps": len(t.steps),
            "tokens": t.tokens,
            "latency_ms": t.latency_ms,
            "cost": t.cost_usd,
        })
    return rows
