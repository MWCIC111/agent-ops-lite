"""2_工具分析.py — Function Calling 工具维度的健康度分析

对应简历卖点：Function Calling / MCP 工具调用工程化。
展示每个工具的调用次数、成功率、平均耗时——生产中"哪个工具拖垮了整体"一眼可见。
"""
import streamlit as st
from common import show_clock, page_visit
from op_log import log_operation
import pandas as pd
import plotly.express as px

from demo_data import load_traces

st.set_page_config(page_title="工具分析 · agent-ops-lite", layout="wide")
st.title("工具分析")
show_clock()
page_visit("工具分析")
st.caption("各工具（Function Calling）的调用量 / 成功率 / 平均耗时")

traces, mode = load_traces()
if mode == "real":
    st.success("🟢 真实数据：工具调用来自真实 Agent 运行（BM25 检索 / 垂直 Agent 等）。")
else:
    st.warning("🟡 模拟数据：数据库为空，当前为可复现模拟数据。播种真实数据后自动切换。")
rows = []
for t in traces:
    for s in t.steps:
        if s.tool:
            rows.append(
                {"tool": s.tool, "status": s.status, "latency_ms": s.latency_ms}
            )
df = pd.DataFrame(rows)

agg = (
    df.groupby("tool")
    .agg(
        调用次数=("status", "size"),
        成功次数=("status", lambda x: (x == "success").sum()),
        平均耗时ms=("latency_ms", "mean"),
    )
    .reset_index()
)
agg["成功率"] = (agg["成功次数"] / agg["调用次数"]).map("{:.1%}".format)
agg["平均耗时s"] = (agg["平均耗时ms"] / 1000).round(2)
agg = agg.rename(columns={"tool": "工具", "latency_ms": "latency_ms"})

c1, c2 = st.columns(2)
with c1:
    fig = px.bar(
        agg, x="工具", y="调用次数", color="工具",
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig.update_layout(height=360, margin=dict(t=10, b=10, l=10, r=10), showlegend=False,
                      xaxis_title="", yaxis_title="调用次数")
    st.plotly_chart(fig, width="stretch")
with c2:
    fig2 = px.bar(
        agg, x="工具", y="平均耗时ms", color="工具",
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig2.update_layout(height=360, margin=dict(t=10, b=10, l=10, r=10), showlegend=False,
                       xaxis_title="", yaxis_title="平均耗时(ms)")
    st.plotly_chart(fig2, width="stretch")

st.subheader("工具明细")
st.dataframe(
    agg[["工具", "调用次数", "成功率", "平均耗时s", "平均耗时ms"]],
    width="stretch",
    hide_index=True,
)
