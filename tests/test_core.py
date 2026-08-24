"""test_core.py — agent_ops 核心库验证脚本

覆盖：
  1. @trace 自动采集（trace_id / agent / 步骤数）
  2. record_step 记录步骤字段
  3. 函数抛异常 → Trace 自动标记 failed
  4. report() 聚合统计正确
  5. traces_to_rows() 输出与面板表格字段一致
  6. 数据结构与 app/demo_data.py 兼容（字段对齐）
  7. span() 父子 span 嵌套（子步骤挂载 + 递归聚合）
  8. model_usage() 按模型归因（含子步骤 token/成本）

运行：
    python examples/../tests/test_core.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_ops import (
    AlertRule,
    Collector,
    MemoryStore,
    SQLiteStore,
    WebhookAlert,
    get_collector,
    model_usage,
    record_step,
    report,
    send_alert,
    span,
    trace,
    traces_to_rows,
)

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


def main() -> None:
    get_collector().clear()

    # ---------- 1/2. 正常采集 ----------
    @trace(agent="检索 Agent", model="qwen-max")
    def ok_agent(q: str) -> str:
        record_step("意图识别", model="qwen-plus", tokens_in=300, tokens_out=80, latency_ms=350)
        record_step("知识检索", tool="知识库检索", tokens_in=800, tokens_out=120, latency_ms=1250)
        return "ok"

    ok_agent("什么是 AgentOps?")
    traces = get_collector().traces()
    print("== 正常采集 ==")
    check("采集到 1 条 Trace", len(traces) == 1, f"got {len(traces)}")
    t = traces[0]
    check("trace_id 已生成", bool(t.trace_id))
    check("agent 名正确", t.agent == "检索 Agent")
    check("2 个步骤已记录", len(t.steps) == 2, f"got {len(t.steps)}")
    check("Trace 状态 success", t.status == "success")
    check("token 聚合正确", t.tokens == 300 + 80 + 800 + 120, f"got {t.tokens}")
    check("成本已核算", t.cost_usd > 0)
    check("延迟已核算", t.latency_ms == 350 + 1250, f"got {t.latency_ms}")

    # ---------- 3. 失败自动标记 ----------
    get_collector().clear()

    @trace(agent="失败 Agent")
    def bad_agent(q: str) -> str:
        record_step("意图识别", tokens_in=100, tokens_out=50, latency_ms=200)
        raise RuntimeError("工具超时")

    try:
        bad_agent("触发失败")
    except RuntimeError:
        pass

    print("== 失败自动标记 ==")
    traces = get_collector().traces()
    check("失败调用也采集了 Trace", len(traces) == 1)
    check("Trace 标记为 failed", traces[0].status == "failed", f"got {traces[0].status}")
    check("失败原因写入步骤", any(s.status == "error" and "工具超时" in (s.error or "") for s in traces[0].steps))

    # ---------- 4. report 聚合 ----------
    print("== report 聚合 ==")
    r = report()
    check("total.calls 正确", r["total"]["calls"] == 1, f"got {r['total']['calls']}")
    check("成功率 0%（唯一调用失败）", r["total"]["success_rate"] == 0.0, f"got {r['total']['success_rate']}")
    check("by_agent 有分组", "失败 Agent" in r["by_agent"])

    # ---------- 5. traces_to_rows 与面板字段一致 ----------
    print("== traces_to_rows 兼容性 ==")
    rows = traces_to_rows()
    expected_cols = {"trace_id", "agent", "time", "status", "n_steps", "tokens", "latency_ms", "cost"}
    check("字段集合与面板表格一致", expected_cols == set(rows[0].keys()), f"got {set(rows[0].keys())}")

    # ---------- 7. span 父子 span 嵌套 ----------
    print("== span 父子 span ==")
    get_collector().clear()

    @trace(agent="RAG Agent")
    def rag_agent(q: str) -> str:
        with span("RAG 检索链路", model="qwen-plus"):
            record_step("向量检索", tool="Milvus", tokens_in=300, tokens_out=100, latency_ms=200)
            record_step("精排", tool="Rerank", tokens_in=100, tokens_out=20, latency_ms=80)
        record_step("内容生成", model="qwen-max", tokens_in=500, tokens_out=200, latency_ms=600)
        return "ok"

    rag_agent("测试")
    t = get_collector().traces()[0]
    check("顶层 2 步（RAG链路 + 生成）", len(t.steps) == 2, f"got {len(t.steps)}")
    parent = t.steps[0]
    check("span 父步骤有 2 个子步骤", len(parent.children) == 2, f"got {len(parent.children)}")
    check("子步骤名称正确", [c.name for c in parent.children] == ["向量检索", "精排"])
    check("子步骤工具正确", [c.tool for c in parent.children] == ["Milvus", "Rerank"])
    # 递归聚合：父步骤 token 不含子（自身 0），Trace 总 token 含全部
    check("Trace token 递归聚合", t.tokens == 300 + 100 + 100 + 20 + 500 + 200, f"got {t.tokens}")
    check("Trace 延迟递归聚合", t.latency_ms == 200 + 80 + 600, f"got {t.latency_ms}")
    check("Trace 成本递归聚合", t.cost_usd > 0)

    # ---------- 8. model_usage 按模型归因 ----------
    print("== model_usage 按模型归因 ==")
    u = model_usage()
    check("qwen-plus 归因 3 次调用", u.get("qwen-plus", {}).get("calls") == 3,
          f"got {u.get('qwen-plus', {}).get('calls')}")
    check("qwen-plus token 归因正确", u["qwen-plus"]["tokens"] == 300 + 100 + 100 + 20,
          f"got {u['qwen-plus']['tokens']}")
    check("qwen-max 归因 1 次调用", u.get("qwen-max", {}).get("calls") == 1,
          f"got {u.get('qwen-max', {}).get('calls')}")
    check("by_model 出现在 report()", "by_model" in report())
    check("report by_model 与 model_usage 一致", report()["by_model"]["qwen-max"]["calls"] == 1)

    # ---------- 9. 告警 Webhook ----------
    print("== 告警 Webhook ==")

    # 9.1 AlertRule 边界
    r_gt = AlertRule("error_rate", ">", 0.10, "错误率超 10%")
    check("AlertRule '>' 未超不触发", not r_gt.hit(0.10))
    check("AlertRule '>' 超过触发", r_gt.hit(0.11))
    r_ge = AlertRule("total_cost_usd", ">=", 1.0, "成本超 $1")
    check("AlertRule '>=' 边界触发", r_ge.hit(1.0))

    # 9.2 check() 触发/不触发
    alert = WebhookAlert(webhook_url="http://127.0.0.1:1/mock", rules=[r_gt, r_ge])
    ok_rep = {"total": {"error_rate": 0.03, "total_cost_usd": 0.5}, "window": "近 10 次调用"}
    bad_rep = {"total": {"error_rate": 0.25, "total_cost_usd": 2.3}, "window": "近 10 次调用"}
    check("正常指标不触发告警", len(alert.check(ok_rep)) == 0)
    events = alert.check(bad_rep)
    check("超限指标触发 2 条告警", len(events) == 2, f"got {len(events)}")
    check("告警事件带规则标签", all(ev.rule.label for ev in events))

    # 9.3 send() 用本地 mock HTTP 服务验证真实 POST（企业微信格式）
    import http.server
    import json as _json
    import threading

    received: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(_json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"errcode":0}')

        def log_message(self, *a):  # 静默
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        url = f"http://127.0.0.1:{port}/robot"
        alert_wx = WebhookAlert(webhook_url=url, rules=[r_gt, r_ge])
        sent = alert_wx.check_and_send(bad_rep)
        check("check_and_send 发送成功 2 条", len(sent) == 2, f"got {len(sent)}")
        check("企业微信 payload 格式正确", received and received[0]["msgtype"] == "text"
              and "content" in received[0]["text"], f"got {received[:1]}")

        alert_fs = WebhookAlert(webhook_url=url, webhook_type="feishu", rules=[r_gt])
        alert_fs.check_and_send(bad_rep)
        check("飞书 payload 格式正确", len(received) == 3 and received[2]["msg_type"] == "text"
              and "text" in received[2]["content"], f"got {received[2:]}")
    finally:
        srv.shutdown()

    # 9.4 禁用时不发送
    alert_off = WebhookAlert(webhook_url="http://127.0.0.1:1/mock", enabled=False)
    check("enabled=False 不发送", alert_off.send(events[0]) is False)

    # 9.5 send_alert 便捷函数（连接失败返回 False，不抛异常）
    check("send_alert 失败返回 False", send_alert("http://127.0.0.1:1/mock", "test") is False)

    # ---------- 10. SQLite 持久化存储 ----------
    print("== SQLite 持久化存储 ==")

    import tempfile
    import os as _os

    tmpdir = tempfile.mkdtemp(prefix="agentops_sqlite_")
    db_path = _os.path.join(tmpdir, "test_ops.db")

    # 10.1 独立 store 往返（含父子 span 树）
    store = SQLiteStore(db_path)
    from agent_ops import Step, Trace, dict_to_trace, trace_to_dict
    from datetime import datetime as _dt

    tr = Trace(
        trace_id="abc123", agent="测试 Agent", started_at=_dt(2026, 8, 24, 10, 0, 0),
        status="success", tokens=300, latency_ms=500, cost_usd=0.012,
        steps=[
            Step("检索", "qwen-plus", "知识库", 200, 80, 300, "success",
                 children=[Step("向量检索", "qwen-plus", "Milvus", 150, 60, 200, "success")]),
            Step("生成", "qwen-max", None, 100, 60, 200, "success"),
        ],
    )
    store.save(tr)
    check("SQLiteStore 落库 1 条", store.count() == 1, f"got {store.count()}")
    loaded = store.load()
    check("load() 还原 Trace 字段", loaded[0].trace_id == "abc123"
          and loaded[0].agent == "测试 Agent" and loaded[0].status == "success")
    check("父子 span 树还原", len(loaded[0].steps) == 2
          and loaded[0].steps[0].children[0].name == "向量检索",
          f"got {[s.name for s in loaded[0].steps]}")
    check("子步骤模型/token 还原", loaded[0].steps[0].children[0].model == "qwen-plus"
          and loaded[0].steps[0].children[0].tokens_in == 150)
    check("聚合值还原", loaded[0].tokens == 300 and loaded[0].cost_usd == 0.012)
    check("trace_to_dict 往返一致", dict_to_trace(trace_to_dict(tr)).trace_id == "abc123")

    # 10.2 Collector(storage=...) 集成：add 自动落库（同 trace_id 幂等覆盖，新 trace_id 追加）
    tr2 = Trace(
        trace_id="def456", agent="测试 Agent2", started_at=_dt(2026, 8, 24, 11, 0, 0),
        status="failed", tokens=100, latency_ms=200, cost_usd=0.001,
        steps=[Step("异常", "qwen-plus", None, 100, 0, 200, "error", "timeout")],
    )
    store2 = SQLiteStore(db_path)
    col = Collector(storage=store2)
    col.add(tr)
    col.add(tr2)
    check("Collector.add 自动落库（幂等+新增）", store2.count() == 2, f"got {store2.count()}")

    # 10.3 模拟"重启"：新 Collector + 同一存储 → 从库恢复历史
    fresh = Collector(storage=store2)
    hist = fresh.traces()
    check("重启后从存储恢复历史", len(hist) == 2, f"got {len(hist)}")
    check("恢复的 Trace 可被 report 聚合", report(fresh)["total"]["calls"] == 2,
          f"got {report(fresh)['total']['calls']}")

    # 10.4 clear 同时清空内存与存储
    fresh.clear()
    check("clear 清空存储", store2.count() == 0 and len(fresh.traces()) == 0)

    # 10.5 默认 Collector 行为不受影响（无 storage 仍纯内存）
    plain = Collector()
    plain.add(tr)
    check("无 storage 的 Collector 正常", len(plain.traces()) == 1)

    # 10.6 MemoryStore 协议实现
    ms = MemoryStore()
    ms.save(tr)
    check("MemoryStore 保存/加载", len(ms.load()) == 1)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------- 6. 与 demo_data 数据结构兼容 ----------
    print("== 与 app/demo_data.py 兼容 ==")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
    from demo_data import Step as DemoStep, Trace as DemoTrace, MODEL_PRICE as DEMO_PRICE

    from agent_ops import MODEL_PRICE, Step, Trace

    def fields(cls):
        import dataclasses
        return {f.name for f in dataclasses.fields(cls)}

    check("Step 字段与面板一致", fields(Step) == fields(DemoStep),
          f"agent_ops={fields(Step)} demo={fields(DemoStep)}")
    check("Trace 字段与面板一致", fields(Trace) == fields(DemoTrace),
          f"agent_ops={fields(Trace)} demo={fields(DemoTrace)}")
    check("模型单价与面板一致", MODEL_PRICE == DEMO_PRICE)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
