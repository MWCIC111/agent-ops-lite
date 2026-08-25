"""observe_agent.py — agentops-observe skill 配套演示脚本

跑通完整闭环：
  1. @trace 采集（正常 + 故意失败一次），自动落 SQLite
  2. report() / model_usage() 指标聚合
  3. 告警规则命中（本地 mock HTTP 验证 POST 格式）
  4. 模拟重启：新 Collector 从同一存储恢复历史

运行：python scripts/observe_agent.py [--db PATH]
零依赖：仅 Python 标准库。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _REPO_ROOT)

from agent_ops import (
    AlertRule,
    Collector,
    SQLiteStore,
    WebhookAlert,
    model_usage,
    record_step,
    report,
    span,
    trace,
)

LINE = "=" * 60


def build_demo_traces(collector: Collector) -> None:
    """用真实调用构造 2 条 Trace：一条成功（含父子 span），一条失败。"""

    @trace(agent="检索 Agent", model="qwen-max", collector=collector)
    def ok_agent(question: str) -> str:
        with span("RAG 检索链路", model="qwen-plus"):
            record_step("向量检索", tool="Milvus", tokens_in=300, tokens_out=100, latency_ms=200)
            record_step("精排", tool="Rerank", tokens_in=100, tokens_out=20, latency_ms=80)
        record_step("内容生成", model="qwen-max", tokens_in=500, tokens_out=200, latency_ms=600)
        return "ok"

    @trace(agent="校验 Agent", model="qwen-plus", collector=collector)
    def bad_agent(question: str) -> str:
        record_step("结论校验", model="qwen-plus", tokens_in=200, tokens_out=60, latency_ms=250)
        raise RuntimeError("依据回溯失败: 结论未指向任何检索片段")

    ok_agent("什么是 AgentOps?")
    try:
        bad_agent("校验失败样本")
    except RuntimeError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="agentops-observe 闭环演示")
    parser.add_argument("--db", default="", help="SQLite 文件路径（默认临时目录）")
    args = parser.parse_args()

    db_path = args.db or os.path.join(tempfile.mkdtemp(prefix="agentops_skill_"), "ops.db")
    store = SQLiteStore(db_path)

    print(LINE)
    print("步骤 1/4  采集：@trace + record_step（含父子 span + 一次故意失败）")
    print(LINE)
    col = Collector(storage=store)
    build_demo_traces(col)
    print(f"  采集完成，共 {len(col.traces())} 条 Trace（其中 1 条 failed），已自动落库")

    print()
    print(LINE)
    print("步骤 2/4  聚合：report() + model_usage()")
    print(LINE)
    r = report(col)
    print(f"  total.calls        = {r['total']['calls']}")
    print(f"  total.success_rate = {r['total']['success_rate']:.0%}")
    print(f"  total.cost_usd     = ${r['total']['total_cost_usd']:.4f}")
    print(f"  total.avg_latency  = {r['total']['avg_latency_ms']:.0f} ms")
    print("  按模型归因（成本降序）:")
    for model, u in model_usage(col).items():
        print(f"    {model}: calls={u['calls']} tokens={u['tokens']} cost=${u['cost_usd']:.4f}")

    print()
    print(LINE)
    print("步骤 3/4  告警：阈值命中 + 本地 mock HTTP 验证 POST 格式")
    print(LINE)
    import http.server
    import json
    import threading

    received: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"errcode":0}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rules = [
            AlertRule("error_rate", ">", 0.10, "错误率超 10%"),
            AlertRule("total_cost_usd", ">=", 1.0, "成本超 $1"),
        ]
        alert = WebhookAlert(webhook_url=f"http://127.0.0.1:{srv.server_address[1]}/robot",
                             webhook_type="wecom", rules=rules)
        events = alert.check_and_send(r)
        print(f"  触发 {len(events)} 条告警事件:")
        for ev in events:
            print(f"    - [{ev.rule.metric}] {ev.rule.label}")
        print(f"  mock 服务收到 {len(received)} 个 POST，payload 格式: msgtype={received[0]['msgtype']}")
    finally:
        srv.shutdown()

    print()
    print(LINE)
    print("步骤 4/4  持久化：模拟重启 → 新 Collector 从同一存储恢复")
    print(LINE)
    fresh = Collector(storage=SQLiteStore(db_path))
    hist = fresh.traces()
    print(f"  重启后恢复 {len(hist)} 条 Trace:")
    for t in hist:
        print(f"    {t.trace_id}  {t.agent}  status={t.status}  tokens={t.tokens}  cost=${t.cost_usd:.4f}")
    agg = report(fresh)
    print(f"  恢复数据可被 report 聚合: calls={agg['total']['calls']}")

    print()
    print(LINE)
    print("闭环演示完成  ✅  三个记忆点：3 行接入 / 四问闭环 / 零依赖")
    print(LINE)


if __name__ == "__main__":
    main()
