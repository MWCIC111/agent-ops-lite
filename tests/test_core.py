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
    Collector,
    get_collector,
    model_usage,
    record_step,
    report,
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
