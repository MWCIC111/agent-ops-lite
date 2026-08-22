"""3_成本核算.py — 按 Agent / 模型拆解成本 + 成本配额熔断模拟

对应简历卖点：成本核算与配额熔断（生产 Agent 上线绕不开的硬指标）。
交互亮点：拖动"每日成本配额"滑杆，超限即触发熔断告警——把真实生产机制搬进 Demo。
联动设计：熔断状态写入全局共享状态，首页顶部横幅会同步提示（模拟真实生产中
          配额状态存在 Redis、所有面板统一读取）。
"""
import streamlit as st
import pandas as pd
import plotly.express as px

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_state import init as sim_init, get as sim_get

sim_init()

from demo_data import load_demo_traces

st.set_page_config(page_title="成本核算 · agent-ops-lite", layout="wide")
st.title("成本核算")
st.caption("按 Agent / 日期维度拆解调用成本（¥），并模拟成本配额熔断")

traces = load_demo_traces()
rows = [
    {
        "agent": t.agent,
        "cost": t.cost_usd * 7.2,
        "tokens": t.tokens,
        "date": t.started_at.strftime("%m-%d"),
    }
    for t in traces
]
df = pd.DataFrame(rows)

# ---- 按 Agent 成本占比 ----
c1, c2 = st.columns(2)
with c1:
    by_agent = df.groupby("agent")["cost"].sum().reset_index()
    fig = px.pie(by_agent, names="agent", values="cost", hole=0.4,
                 color_discrete_sequence=px.colors.qualitative.Set2)
    fig.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10),
                      title="各 Agent 成本占比")
    st.plotly_chart(fig, width="stretch")
with c2:
    daily = df.groupby("date")["cost"].sum().reset_index()
    fig2 = px.area(daily, x="date", y="cost",
                   color_discrete_sequence=["#D85A30"])
    fig2.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10),
                       title="每日成本趋势", xaxis_title="", yaxis_title="成本(¥)")
    st.plotly_chart(fig2, width="stretch")

# ---- 成本配额熔断模拟 ----
st.subheader("成本配额熔断（模拟）")
quota = st.slider("每日成本配额（¥）", min_value=10, max_value=200, value=80, step=5,
                  help="今日成本约 ¥18（14 天模拟数据），把配额拖到 20 以下即可看到熔断效果")
today = df["date"].max()
today_cost = df[df["date"] == today]["cost"].sum()

# 熔断结果写入全局共享状态（首页横幅会同步感知）
sim_get()["quota"] = quota
if today_cost > quota:
    sim_get()["quota_breach"] = True
    st.error(f"⚠️ 今日成本 ¥{today_cost:.1f} 已超出配额 ¥{quota} —— "
             f"模拟触发熔断：拒绝新的 Agent 调用，仅保留高优先级任务。")
else:
    sim_get()["quota_breach"] = False
    st.success(f"今日成本 ¥{today_cost:.1f}，低于配额 ¥{quota}，运行正常。"
               f"（剩余额度 ¥{quota - today_cost:.1f}）")

st.caption("生产实现：配额状态写入 Redis，Agent 入口统一校验，超限即降级/拒绝。")
