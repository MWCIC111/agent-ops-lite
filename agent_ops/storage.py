"""storage.py — 持久化存储后端

默认 Collector 是内存采集。生产场景需要"重启不丢、历史可查"，
通过 storage 接口把 Trace 落库。

内置 SQLiteStore 用 Python 标准库 sqlite3，保持零依赖；
实现 TraceStore 协议即可替换为 Elasticsearch / ClickHouse 等。

用法：
    store = SQLiteStore("agent_ops.db")
    collector = Collector(storage=store)

    @trace(collector=collector)
    def my_agent(...): ...

    重启后：collector.traces() 自动从库中恢复历史。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Protocol

from .tracer import Step, Trace


# ---------------------------------------------------------------------------
# Trace <-> dict 序列化（含嵌套步骤树，保证父子 span 无损往返）
# ---------------------------------------------------------------------------


def _dt_to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _step_to_dict(s: Step) -> dict:
    return {
        "name": s.name,
        "model": s.model,
        "tool": s.tool,
        "tokens_in": s.tokens_in,
        "tokens_out": s.tokens_out,
        "latency_ms": s.latency_ms,
        "status": s.status,
        "error": s.error,
        "children": [_step_to_dict(c) for c in s.children],
    }


def _dict_to_step(d: dict) -> Step:
    return Step(
        name=d["name"],
        model=d["model"],
        tool=d["tool"],
        tokens_in=d["tokens_in"],
        tokens_out=d["tokens_out"],
        latency_ms=d["latency_ms"],
        status=d["status"],
        error=d.get("error"),
        children=[_dict_to_step(c) for c in d.get("children", [])],
    )


def trace_to_dict(t: Trace) -> dict:
    """Trace -> 可 JSON 序列化的 dict"""
    return {
        "trace_id": t.trace_id,
        "agent": t.agent,
        "started_at": _dt_to_iso(t.started_at),
        "status": t.status,
        "tokens": t.tokens,
        "latency_ms": t.latency_ms,
        "cost_usd": t.cost_usd,
        "steps": [_step_to_dict(s) for s in t.steps],
    }


def dict_to_trace(d: dict) -> Trace:
    """dict -> Trace（还原嵌套步骤树）"""
    return Trace(
        trace_id=d["trace_id"],
        agent=d["agent"],
        started_at=_iso_to_dt(d["started_at"]),
        status=d["status"],
        tokens=d.get("tokens", 0),
        latency_ms=d.get("latency_ms", 0),
        cost_usd=d.get("cost_usd", 0.0),
        steps=[_dict_to_step(s) for s in d.get("steps", [])],
    )


# ---------------------------------------------------------------------------
# 存储协议与实现
# ---------------------------------------------------------------------------


class TraceStore(Protocol):
    """存储后端协议：实现 save / load / clear 即可替换为 ES 等。"""

    def save(self, trace: Trace) -> None: ...

    def load(self) -> list[Trace]: ...

    def clear(self) -> None: ...


class MemoryStore:
    """内存存储（等价于 Collector 默认行为），统一实现协议便于替换。"""

    def __init__(self) -> None:
        self._traces: list[Trace] = []

    def save(self, trace: Trace) -> None:
        self._traces.append(trace)

    def load(self) -> list[Trace]:
        return list(self._traces)

    def clear(self) -> None:
        self._traces.clear()


class SQLiteStore:
    """SQLite 持久化存储（零依赖，Python 内置 sqlite3）。

    traces 表保存完整 Trace（含嵌套步骤树，JSON 序列化到 data 列），
    另存常用列便于按 Agent / 时间查询。线程安全。
    """

    def __init__(self, db_path: str = "agent_ops.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # ---- 内部 ----

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS traces (
                        trace_id   TEXT PRIMARY KEY,
                        agent      TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        status     TEXT NOT NULL,
                        tokens     INTEGER NOT NULL DEFAULT 0,
                        latency_ms INTEGER NOT NULL DEFAULT 0,
                        cost_usd   REAL NOT NULL DEFAULT 0,
                        data       TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(agent)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at)")
                conn.commit()
            finally:
                conn.close()

    # ---- TraceStore 协议 ----

    def save(self, trace: Trace) -> None:
        payload = json.dumps(trace_to_dict(trace), ensure_ascii=False)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO traces "
                    "(trace_id, agent, started_at, status, tokens, latency_ms, cost_usd, data) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        trace.trace_id,
                        trace.agent,
                        _dt_to_iso(trace.started_at),
                        trace.status,
                        trace.tokens,
                        trace.latency_ms,
                        trace.cost_usd,
                        payload,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def load(self) -> list[Trace]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT data FROM traces ORDER BY started_at").fetchall()
            finally:
                conn.close()
        return [dict_to_trace(json.loads(r["data"])) for r in rows]

    def clear(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM traces")
                conn.commit()
            finally:
                conn.close()

    # ---- 扩展 ----

    def count(self) -> int:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                row = conn.execute("SELECT COUNT(*) AS c FROM traces").fetchone()
            finally:
                conn.close()
        return int(row[0])
