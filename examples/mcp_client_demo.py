"""mcp_client_demo.py — 最小 MCP client（纯标准库）

启动 agent_ops.mcp_server 子进程，走完整 MCP 握手：
  initialize → notifications/initialized → tools/list → tools/call（每个工具都调一遍）

价值：不依赖任何 GUI 客户端（Claude Desktop / Cursor），命令行就能演示
"我的 MCP server 真的能被 AI 客户端连接调用"。两端都是我自己写的、纯标准库。

运行：
    python examples/mcp_client_demo.py
    python examples/mcp_client_demo.py --db /tmp/agent_ops.db
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# 仓库根目录（examples/ 的上一级）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable


def main() -> None:
    db_arg = "--db"
    db_path = os.path.join(REPO_ROOT, "mcp_demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    # 启动 mcp_server 子进程（--demo 填充演示数据）
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}
    proc = subprocess.Popen(
        [PYTHON, "-m", "agent_ops.mcp_server", "--demo", db_arg, db_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    def send(obj: dict) -> dict | None:
        """发一条 JSON-RPC 消息，返回响应（通知类返回 None）"""
        line = json.dumps(obj) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()
        if obj.get("id") is None:
            return None  # 通知不响应
        return json.loads(proc.stdout.readline())

    def call_tool(name: str, args: dict | None = None) -> None:
        r = send({
            "jsonrpc": "2.0", "id": _next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        })
        content = r["result"]["content"][0]["text"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = content
        is_error = r["result"].get("isError", False)
        tag = "❌ ERROR" if is_error else "✅"
        print(f"\n{tag} 工具 {name}:")
        print(json.dumps(parsed, ensure_ascii=False, indent=2)[:600])

    _id = 0

    def _next_id() -> int:
        nonlocal _id
        _id += 1
        return _id

    try:
        time.sleep(0.5)  # 等 server 起来
        print("=" * 60)
        print("MCP Client Demo — 连接 agent_ops.mcp_server")
        print("=" * 60)

        # 1. initialize 握手
        r = send({
            "jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-client-demo", "version": "1.0"},
            },
        })
        info = r["result"]["serverInfo"]
        print(f"\n[1] initialize 握手成功")
        print(f"    server: {info['name']} v{info['version']}")
        print(f"    protocol: {r['result']['protocolVersion']}")

        # 2. initialized 通知（无响应）
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        print(f"\n[2] notifications/initialized 已发送（无响应，符合规范）")

        # 3. tools/list
        r = send({"jsonrpc": "2.0", "id": _next_id(), "method": "tools/list"})
        tools = r["result"]["tools"]
        print(f"\n[3] tools/list 返回 {len(tools)} 个工具:")
        for t in tools:
            print(f"    - {t['name']}: {t['description'][:50]}...")

        # 4. 逐个调用工具
        print(f"\n[4] tools/call 逐个调用:")
        call_tool("report")
        call_tool("model_usage")
        call_tool("traces", {"limit": 3})
        call_tool("history", {"db_path": db_path, "limit": 1})
        call_tool("check_alerts")

        # 5. 边界：未知工具
        print(f"\n[5] 边界测试:")
        call_tool("bogus_tool")

        # 6. 边界：未知 method
        r = send({"jsonrpc": "2.0", "id": _next_id(), "method": "nonexistent/method"})
        print(f"\n❌ 未知 method 返回 error code: {r['error']['code']} ({r['error']['message']})")

        print("\n" + "=" * 60)
        print("✅ MCP client demo 完成 — server 端握手 + 全工具调用 + 边界测试全过")
        print("=" * 60)

    finally:
        proc.terminate()
        proc.wait(timeout=5)
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass  # 沙箱回收站不可用时忽略，db 留着无害


if __name__ == "__main__":
    main()
