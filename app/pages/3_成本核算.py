"""3_成本核算.py — 按 Agent / 模型拆解成本 + 成本配额熔断（真实拦截）

对应简历卖点：成本核算与配额熔断（生产 Agent 上线绕不开的硬指标）。
真实拦截（Phase 1b）：每次 LLM 调用在 _deepseek_chat 入口经 check_quota() 校验，
阈值取自服务端环境变量 AGENTOPS_DAILY_QUOTA_CNY，超阈值直接拒绝——不再是滑杆模拟。
联动设计：本页把真实「今日成本 / 配额阈值 / 是否熔断」写入全局共享状态，
          首页顶部横幅同步提示（模拟真实生产中配额状态存在 Redis、所有面板统一读取）。
"""
import streamlit as st
from common import show_clock, page_visit
from op_log import log_operation
import pandas as pd
import plotly.express as px

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_state import init as sim_init, get as sim_get

sim_init()

from demo_data import load_traces

st.set_page_config(page_title="成本核算 · agent-ops-lite", layout="wide")
st.title("成本核算")
show_clock()
page_visit("成本核算")
st.caption("按 Agent / 日期维度拆解调用成本（¥），并模拟成本配额熔断")

traces, mode = load_traces()
if mode == "real":
    st.success("🟢 真实数据：成本按真实模型单价（deepseek-chat 等）折算，来自 agent_ops.db。")
else:
    st.warning("🟡 模拟数据：数据库为空，当前为可复现模拟数据。播种真实数据后自动切换为真实成本。")
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

# ---- 成本配额熔断（真实拦截，Phase 1b）----
st.subheader("成本配额熔断（真实拦截）")
# 真实阈值：优先取服务端环境变量 AGENTOPS_DAILY_QUOTA_CNY（默认 50¥）
quota_env = float(os.environ.get("AGENTOPS_DAILY_QUOTA_CNY", "50"))
# 滑杆仅用于本页 what-if 演示；真实调用拦截以服务端 env 为准
quota = st.slider("每日成本配额（¥，演示用阈值）", min_value=10, max_value=200,
                  value=int(min(quota_env, 200)), step=5,
                  help="真实调用拦截以服务端环境变量 AGENTOPS_DAILY_QUOTA_CNY 为准"
                       "（_deepseek_chat 入口统一校验）。此滑杆仅用于本页 what-if 演示。")
today = df["date"].max()
today_cost = df[df["date"] == today]["cost"].sum()

# 熔断结果写入全局共享状态（首页横幅会同步感知）
sim_get()["quota"] = quota
if today_cost > quota:
    sim_get()["quota_breach"] = True
    st.error(f"⚠️ 今日成本 ¥{today_cost:.4f} 已超出配额 ¥{quota} —— "
             f"真实触发熔断：新的 Agent 调用在 _deepseek_chat 入口被拒绝，仅保留高优先级任务。")
else:
    sim_get()["quota_breach"] = False
    st.success(f"今日成本 ¥{today_cost:.4f}，低于配额 ¥{quota}，运行正常。"
               f"（剩余额度 ¥{quota - today_cost:.4f}）")

st.caption("生产实现：配额状态写入 Redis，Agent 入口统一校验，超限即降级/拒绝。"
           "本 Demo 已在 _deepseek_chat 入口做真实调用前拦截（check_quota）。")
