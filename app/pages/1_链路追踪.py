"""1_链路追踪.py — 按 Trace ID 查看一次 Agent 调用的完整执行链

对应简历卖点：Trace ID 全链路可观测、异常定位从小时级缩短到分钟级。
失败步骤红色高亮；重试步骤可见（真实生产中最难排查的就是"失败→重试"链路）。
"""
import streamlit as st
import pandas as pd

from demo_data import load_traces

st.set_page_config(page_title="链路追踪 · agent-ops-lite", layout="wide")
st.title("链路追踪")
st.caption("输入 Trace ID 查看一次 Agent 调用的完整执行链（含失败与重试步骤）")

traces, mode = load_traces()
if mode == "real":
    st.success("🟢 真实数据：来自 agent_ops.db 的真实 LLM 调用 Trace。")
else:
    st.warning("🟡 模拟数据：数据库为空，当前为可复现模拟 Trace。运行「真实 Agent」或「数据管理」播种真实数据后自动切换。")

# 按 Agent 过滤，避免 Trace 多了之后淹没在 selectbox 里
agents = sorted(set(t.agent for t in traces))
c1, c2 = st.columns([1, 2])
with c1:
    selected_agent = st.selectbox("按 Agent 过滤", ["全部"] + agents)
filtered_traces = (
    traces if selected_agent == "全部" else [t for t in traces if t.agent == selected_agent]
)
trace_map = {t.trace_id: t for t in filtered_traces}
with c2:
    trace_id = st.selectbox("选择 Trace ID", list(trace_map.keys()))
t = trace_map[trace_id]
c2.caption(f"当前共 {len(filtered_traces)} 条 Trace")

# ---- 概览卡片 ----
ok = t.status == "success"
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Agent", t.agent)
c2.metric("状态", "成功" if ok else "失败",
          delta=None, delta_color="normal")
c3.metric("总耗时", f"{t.latency_ms / 1000:.2f}s")
c4.metric("Token 消耗", f"{t.tokens:,}")
c5.metric("成本", f"¥{t.cost_usd * 7.2:.4f}")

# ---- 步骤明细 ----
st.subheader("执行步骤")
steps_df = pd.DataFrame(
    [
        {
            "#": i + 1,
            "步骤": s.name,
            "模型": s.model,
            "工具": s.tool or "-",
            "输入Token": s.tokens_in,
            "输出Token": s.tokens_out,
            "耗时(ms)": s.latency_ms,
            "状态": "成功" if s.status == "success" else "失败",
            "错误信息": s.error or "-",
        }
        for i, s in enumerate(t.steps)
    ]
)


def style_row(row):
    if row["状态"] == "失败":
        return ["background-color:#501313;color:#F7C1C1"] * len(row)
    return [""] * len(row)


st.dataframe(
    steps_df.style.apply(style_row, axis=1),
    width="stretch",
    hide_index=True,
)

# ---- 失败步骤高亮说明 ----
if t.status == "failed":
    st.warning("该链路包含失败步骤，且已自动重试——生产中这类链路最容易隐藏问题，"
               "Trace ID 就是用来快速定位它的。")
else:
    st.success("该链路全部步骤成功。")
