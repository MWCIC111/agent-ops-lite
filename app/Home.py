"""Home.py — 总览 Dashboard（agent-ops-lite Live Demo 第一页）

本地运行（在 app/ 目录下）：
    pip install -r requirements.txt
    streamlit run Home.py

这是"最小 Demo"版本：只做 4 个 KPI + 1 个趋势图，
跑通之后再按 roadmap 逐页增加。
"""
import streamlit as st
import pandas as pd
import plotly.express as px

from demo_data import load_demo_traces

st.set_page_config(page_title="agent-ops-lite · 总览", layout="wide")

# ---------- 数据加载 ----------
traces = load_demo_traces()
rows = [
    {
        "trace_id": t.trace_id,
        "agent": t.agent,
        "time": t.started_at,
        "date": t.started_at.strftime("%m-%d"),
        "status": t.status,
        "tokens": t.tokens,
        "latency_ms": t.latency_ms,
        "cost": t.cost_usd * 7.2,  # 演示用固定汇率：美元 → 人民币
        "n_steps": len(t.steps),
    }
    for t in traces
]
df = pd.DataFrame(rows)

# ---------- 标题 ----------
st.title("agent-ops-lite · 总览 v0.1.0")
st.caption("Agent 调用可观测面板（模拟数据）—— 接入真实数据源即可用于生产")

# ---------- 4 个 KPI ----------
today = df["date"].max()
today_df = df[df["date"] == today]

c1, c2, c3, c4 = st.columns(4)
c1.metric("今日调用量", f"{len(today_df):,}")
c2.metric("Token 消耗", f"{today_df['tokens'].sum() / 1e6:.2f}M")
c3.metric("今日成本", f"¥{today_df['cost'].sum():.1f}")
c4.metric("平均延迟", f"{df['latency_ms'].mean() / 1000:.2f}s")

# ---------- 近 14 天调用量趋势 ----------
st.subheader("近 14 天调用量趋势")
daily = df.groupby("date").size().reset_index(name="count")
fig = px.bar(daily, x="date", y="count", color_discrete_sequence=["#378ADD"])
fig.update_layout(
    height=320,
    margin=dict(t=10, b=10, l=10, r=10),
    yaxis_title="调用量",
    xaxis_title="",
)
st.plotly_chart(fig, width="stretch")

# ---------- 最新 Trace ----------
st.subheader("最新链路 Trace")
st.dataframe(
    df.head(20)[["time", "trace_id", "agent", "status", "n_steps", "tokens", "latency_ms", "cost"]],
    width="stretch",
    hide_index=True,
)

st.divider()
st.caption("下一步：新增 链路追踪 / 工具分析 / 成本核算 / 告警异常 页面（见 README Roadmap）")
