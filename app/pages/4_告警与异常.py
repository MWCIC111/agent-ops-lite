"""4_告警与异常.py — 慢调用 Top10 · 错误率趋势 · 失败 Trace 列表

对应简历卖点：生产化告警体系、异常定位从小时级缩短到分钟级。
交互亮点：错误率超过阈值时面板本身"亮红灯"，演示告警规则的真实逻辑。
联动设计：读取全局共享状态——若拓扑页标记了某个 Agent 异常，
          本页会针对该 Agent 单独触发告警（模拟真实监控的 Agent 维度告警）。
"""
import streamlit as st
import pandas as pd
import plotly.express as px

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_state import init as sim_init, get as sim_get

sim_init()

from demo_data import load_traces

st.set_page_config(page_title="告警与异常 · agent-ops-lite", layout="wide")
st.title("告警与异常")
st.caption("慢调用 Top10 · 错误率趋势 · 失败 Trace 列表")

traces, mode = load_traces()
if mode == "real":
    st.success("🟢 真实数据：错误率 / 慢调用来自真实 Agent 运行 Trace（含播种注入的真实失败）。")
else:
    st.warning("🟡 模拟数据：数据库为空，当前为可复现模拟数据。播种真实数据后自动切换。")
rows = [
    {
        "trace_id": t.trace_id,
        "agent": t.agent,
        "time": t.started_at,
        "date": t.started_at.strftime("%m-%d"),
        "status": t.status,
        "latency_ms": t.latency_ms,
    }
    for t in traces
]
df = pd.DataFrame(rows)

# ---- ① 全局联动告警：拓扑页标记的异常 Agent ----
ab = sim_get()["abnormal_agent"]
if ab:
    ab_df = df[df["agent"] == ab]
    ab_rate = (ab_df["status"] == "failed").mean() if len(ab_df) else 0.0
    st.error(
        f"🚨 **联动告警（来自拓扑页）**：{ab} 被标记为异常，"
        f"其历史失败率 {ab_rate:.1%} 已偏离健康基线——"
        f"建议立即检查该 Agent 的模型服务与工具链路，配合 Trace ID 定位根因。"
        f"（真实生产中：拓扑页异常标记来自监控告警，此处为全系统联动的模拟演示。）",
        icon="🚨",
    )

# ---- ② 整体错误率告警 ----
err_rate = (df["status"] == "failed").mean()
if err_rate > 0.15:
    st.error(f"🔴 检测到整体错误率 {err_rate:.1%} > 15% 阈值 —— 触发告警，"
             f"建议检查模型服务与工具链路。")
else:
    st.success(f"整体错误率 {err_rate:.1%}，低于 15% 阈值，系统运行正常。")

# ---- 慢调用 Top10 ----
st.subheader("慢调用 Top 10")
top10 = df.nlargest(10, "latency_ms")
st.dataframe(
    top10[["time", "trace_id", "agent", "latency_ms", "status"]]
    .assign(latency_ms=lambda d: (d["latency_ms"] / 1000).round(2))
    .rename(columns={"time": "时间", "trace_id": "Trace ID", "agent": "Agent",
                     "latency_ms": "耗时(s)", "status": "状态"}),
    width="stretch",
    hide_index=True,
)

# ---- 错误率趋势 ----
st.subheader("错误率趋势（近 14 天）")
err_by_day = (
    df.groupby("date")
    .apply(lambda g: (g["status"] == "failed").mean(), include_groups=False)
    .reset_index(name="error_rate")
)
fig = px.line(err_by_day, x="date", y="error_rate", markers=True,
              color_discrete_sequence=["#E24B4A"])
fig.add_hline(y=0.15, line_dash="dash", line_color="#F09595",
              annotation_text="阈值 15%", annotation_position="top right")
fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10),
                  xaxis_title="", yaxis_title="错误率")
st.plotly_chart(fig, width="stretch")

# ---- 失败 Trace 列表 ----
failed = df[df["status"] == "failed"]
st.subheader(f"失败 Trace（共 {len(failed)} 条）")
st.dataframe(
    failed[["time", "trace_id", "agent", "latency_ms"]]
    .assign(latency_ms=lambda d: (d["latency_ms"] / 1000).round(2))
    .rename(columns={"time": "时间", "trace_id": "Trace ID", "agent": "Agent",
                     "latency_ms": "耗时(s)"}),
    width="stretch",
    hide_index=True,
)
