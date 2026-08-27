"""mcp_server.py — 把 agent-ops-lite 暴露为标准 MCP Server（零依赖，纯标准库）

MCP (Model Context Protocol) 是一套让 AI 应用通过 JSON-RPC 2.0 over stdio
调用外部工具的开放协议。本文件用 Python 标准库手写实现 MCP stdio server，
不依赖任何第三方 MCP SDK——把"核心库零依赖"的约定延伸到协议层。

支持的工具（对应 agent_ops 核心 API）：
  - report       生成聚合报告（总体/按Agent/按模型，与面板口径一致）
  - model_usage  按模型归因的用量统计（成本降序）
  - traces       最近 Trace 列表（展平成行，与面板 1_链路追踪 一致）
  - history      查询 SQLite 持久化的历史 Trace
  - check_alerts 检查告警规则是否触发（不发送，返回触发事件）

用法：
  # 直接运行（stdio 模式，供 MCP client 连接）
  python -m agent_ops.mcp_server

  # 或作为库导入
  from agent_ops.mcp_server import MCPAgentOpsServer, handle_message
  注意：日志请走 stderr（sys.stderr）；stdout 只输出 JSON-RPC 消息。

协议（JSON-RPC 2.0 over stdio，一请求一响应）：
  -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
  <- {"jsonrpc":"2.0","id":1,"result":{...}}
  -> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  <- {"jsonrpc":"2.0","id":2,"result":{"tools":[...]}}
  -> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"report","arguments":{}}}
  <- {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"..."}]}}
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Callable

from .alerts import AlertRule, WebhookAlert
from .metrics import model_usage, report, traces_to_rows
from .storage import SQLiteStore
from .tracer import Trace, get_collector


SERVER_NAME = "agent-ops-lite"
SERVER_VERSION = "0.3.0"

# MCP 协议版本（2025-06-18 为当前主流 client 广泛接受的版本）
PROTOCOL_VERSION = "2025-06-18"


def _datetime_to_iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _value(d, key, default=None):
    """安全取值：既兼容 dict['key']，也兼容对象属性"""
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)


def _to_jsonable(value):
    """把任意返回值（pathlib/datetime/自定义对象）转为 JSON 可序列化结构"""
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, Trace):
        from .storage import trace_to_dict
        return trace_to_dict(value)
    if hasattr(value, "isoformat"):  # datetime / date
        return value.isoformat()
    return value


def build_tools() -> list[dict]:
    """返回 MCP tools/list 需要的工具定义列表"""
    return [
        {
            "name": "report",
            "description": "生成 AgentOps 聚合报告：总调用量/成功率/错误率/平均延迟/总token/总成本，按Agent和按模型分组。与观测面板 KPI 口径一致。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": "ISO 8601 时间。只统计该时间点之后的 Trace。缺省统计全部。",
                    }
                },
            },
        },
        {
            "name": "model_usage",
            "description": "按模型归因的用量统计：调用次数/token入/token出/成本，成本降序。父子 span 的 token 会正确归因到实际模型。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "traces",
            "description": "列出最近 Trace（展平成行：trace_id/agent/time/status/步数/token/延迟/成本），与面板 1_链路追踪 表格字段一致。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回条数上限。缺省全部。",
                    }
                },
            },
        },
        {
            "name": "history",
            "description": "查询 SQLite 持久化的历史 Trace（含完整嵌套步骤树）。生产场景重启后可用此工具追溯历史。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "db_path": {
                        "type": "string",
                        "description": "SQLite 文件路径。缺省 agent_ops.db。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数上限。缺省全部。",
                    },
                },
            },
        },
        {
            "name": "check_alerts",
            "description": "检查告警规则是否触发（错误率/成本/延迟超阈值），返回触发事件列表。只检查不发送——生产由推送通道执行。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "error_rate_threshold": {
                        "type": "number",
                        "description": "错误率阈值 0~1，缺省 0.1（10%）。",
                    },
                    "cost_threshold_usd": {
                        "type": "number",
                        "description": "总成本阈值（美元），缺省 1.0。",
                    },
                },
            },
        },
    ]


class MCPAgentOpsServer:
    """MCP stdio server：读取 stdin 的 JSON-RPC 请求，把响应写到 stdout。"""

    def __init__(self, collector=None, storage=None):
        self._collector = collector or get_collector()
        self._tools = build_tools()
        self._storage = storage
        self._methods: dict[str, Callable] = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "notifications/initialized": self._handle_notification,
        }
        self._log(f"server ready, collector traces={len(self._collector.traces())}")

    # ---- 内部 ----

    def _log(self, msg: str) -> None:
        # 日志必须走 stderr；stdout 留给 JSON-RPC 消息
        print(f"[mcp] {msg}", file=sys.stderr)

    def _handle_initialize(self, params: dict | None) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _handle_tools_list(self, params: dict | None) -> dict:
        return {"tools": self._tools}

    def _handle_notification(self, params: dict | None) -> None:
        # notifications/initialized 不需要响应（无 id）
        return None

    def _handle_tools_call(self, params: dict | None) -> dict:
        params = params or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return self._error_result(f"未知工具: {name}（可用: {', '.join(t['name'] for t in self._tools)}）")
        try:
            text = handler(**arguments)
        except TypeError as exc:
            return self._error_result(f"工具 {name} 参数错误: {exc}")
        except Exception as exc:  # noqa: BLE001 —— MCP 层兜底，不把异常抛给 client
            return self._error_result(f"工具 {name} 执行失败: {type(exc).__name__}: {exc}")
        # 文本统一序列化为 JSON，方便 client 结构化消费
        return {"content": [{"type": "text", "text": json.dumps(text, ensure_ascii=False, default=str)}]}

    @staticmethod
    def _error_result(message: str) -> dict:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": message}, ensure_ascii=False)}],
            "isError": True,
        }

    # ---- 工具实现 ----

    def _tool_report(self, since: str | None = None) -> dict:
        if since:
            from datetime import datetime
            r = report(self._collector, since=datetime.fromisoformat(since))
        else:
            r = report(self._collector)
        return _to_jsonable(r)

    def _tool_model_usage(self) -> dict:
        return _to_jsonable(model_usage(self._collector))

    def _tool_traces(self, limit: int | None = None) -> list:
        # 注意：traces_to_rows 签名是 (traces=None, collector=None)，必须用关键字参数
        rows = traces_to_rows(collector=self._collector)
        if limit is not None:
            rows = rows[-int(limit):]
        return _to_jsonable(rows)

    def _tool_history(self, db_path: str = "agent_ops.db", limit: int | None = None) -> list:
        store = SQLiteStore(db_path)
        traces = store.load()
        if limit is not None:
            traces = traces[-int(limit):]
        return _to_jsonable(traces)

    def _tool_check_alerts(
        self,
        error_rate_threshold: float = 0.1,
        cost_threshold_usd: float = 1.0,
    ) -> dict:
        r = report(self._collector)
        rules = [
            AlertRule(metric="error_rate", op=">", threshold=float(error_rate_threshold)),
            AlertRule(metric="total_cost_usd", op=">", threshold=float(cost_threshold_usd)),
        ]
        webhook = WebhookAlert(rules=rules, webhook_url="")  # 只 check 不 send
        events = webhook.check(r)
        return _to_jsonable(
            {
                "triggered": bool(events),
                "events": [
                    {
                        "metric": e.rule.metric,
                        "op": e.rule.op,
                        "threshold": e.rule.threshold,
                        "value": e.value,
                        "window": e.window,
                    }
                    for e in events
                ],
                "window": r["window"],
            }
        )

    # ---- 协议分发 ----

    def handle_message(self, raw: str) -> str | None:
        """处理一条 JSON-RPC 消息，返回响应 JSON 字符串；通知类返回 None。"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            )
        method = msg.get("method", "")
        msg_id = msg.get("id")
        handler = self._methods.get(method)
        if handler is None:
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
            return json.dumps(resp, ensure_ascii=False)
        try:
            result = handler(msg.get("params"))
        except Exception as exc:  # noqa: BLE001 —— 协议层兜底
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
            return json.dumps(resp, ensure_ascii=False)
        if msg_id is None:
            return None  # 通知（notification）不响应
        return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False, default=str)

    def serve_forever(self) -> None:
        """阻塞读取 stdin，逐行处理 JSON-RPC 消息。"""
        self._log("serving on stdio, waiting for messages...")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            self._log(f"<< {line[:120]}")
            resp = self.handle_message(line)
            if resp is not None:
                print(resp, flush=True)
                self._log(f">> {resp[:120]}")


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def seed_demo_data(collector=None, n: int = 4, db_path: str | None = None) -> None:
    """往采集器填充演示 Trace（含父子 span / 失败 / 多模型），便于本地演示 MCP。

    与 app/demo_data.py 同源数据结构，保证"MCP 查到的数据"与面板展示一致。
    """
    from datetime import datetime, timedelta

    from .tracer import Trace, record_step, span, trace

    col = collector or get_collector()
    # 清空已有数据，保证可重复演示
    col.clear()

    @trace(agent="检索 Agent", collector=col)
    def _retrieval():
        with span("RAG 检索链路", model="qwen-plus"):
            record_step("向量检索", model="qwen-plus", tool="Milvus", tokens_in=300, tokens_out=100, latency_ms=80)
            record_step("精排", model="qwen-plus", tool="Rerank", tokens_in=200, tokens_out=80, latency_ms=60)
        record_step("生成", model="qwen-max", tokens_in=500, tokens_out=120, latency_ms=650)

    @trace(agent="推理 Agent", collector=col)
    def _inference():
        record_step("工具调用", model="qwen-max", tool="SQL查询", tokens_in=280, tokens_out=90, latency_ms=400)
        record_step("缓存命中", model="qwen-plus", tokens_in=0, tokens_out=0, latency_ms=15)

    @trace(agent="失败示例 Agent", collector=col)
    def _failed():
        record_step("知识检索", model="qwen-plus", tool="知识库", tokens_in=150, tokens_out=0, latency_ms=500)
        raise RuntimeError("工具超时")

    for i in range(n):
        _retrieval()
        _inference()
        if i % 2 == 0:
            try:
                _failed()
            except RuntimeError:
                pass

    # 把其中一条 Trace 落库（演示 history 工具）
    if db_path:
        store = SQLiteStore(db_path)
        for t in col.traces()[:2]:
            store.save(t)
        col._storage = None  # 避免 history 复用全局 collector 造成混淆

    return col


def main() -> None:
    """CLI 入口：直接以 stdio server 模式运行。

    参数：
      --demo    启动时填充演示 Trace（含父子 span / 失败 / 多模型），
                方便 Claude Desktop / Cursor 连上即可看到真实结构的数据。
      --db PATH 把前 2 条演示 Trace 落库到指定 SQLite（供 history 工具演示）。
    """
    args = [a for a in sys.argv[1:]]
    use_demo = "--demo" in args
    db_path = None
    if "--db" in args:
        idx = args.index("--db")
        if idx + 1 < len(args):
            db_path = args[idx + 1]

    collector = get_collector()
    if use_demo:
        seed_demo_data(collector=collector, n=4, db_path=db_path)

    server = MCPAgentOpsServer(collector=collector)
    server.serve_forever()


if __name__ == "__main__":
    main()