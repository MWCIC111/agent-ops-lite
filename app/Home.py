"""Home.py — 总览 Dashboard（agent-ops-lite Live Demo 第一页）

本地运行（在 app/ 目录下）：
    pip install -r requirements.txt
    streamlit run Home.py

总览页支持两种模式：
    · 静态模式：近 14 天聚合视图（默认）
    · Live 实时模式：每 1 秒模拟新调用流入，KPI 与趋势图实时滚动
顶部系统状态横幅汇总跨页联动：拓扑异常 / 配额熔断 / 发布决策 / 灰度进度，
一屏看到全系统健康度（模拟真实生产中所有面板读同一个后端）。
"""
import random
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

from demo_data import AGENTS, load_traces
from shared_state import init as sim_init, get as sim_get

sim_init()

st.set_page_config(page_title="agent-ops-lite · 总览", layout="wide")

# ---------- 数据加载（统一数据源：真实优先）----------
traces, mode = load_traces()
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

# ---------- 实时现实时间（取访问者浏览器本地时间，秒级走动，不耗服务器资源）----------
components.html(
    """
    <div id="liveclock"
         style="font-size:13px;color:#8a8a8a;font-family:ui-monospace,Menlo,Consolas,monospace;"></div>
    <script>
    (function () {
      function pad(n) { return String(n).padStart(2, '0'); }
      function tick() {
        var d = new Date();
        var s = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
                pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
        var el = document.getElementById('liveclock');
        if (el) el.textContent = '🕐 当前现实时间（本地）：' + s;
      }
      tick();
      setInterval(tick, 1000);
    })();
    </script>
    """,
    height=30,
)
if mode == "real":
    st.success("🟢 **真实数据模式**：当前展示均来自 `agent_ops.db` 的真实 LLM 调用 Trace"
               "（真实 token / 延迟 / 成本 / 工具 / 知识库召回）。")
else:
    st.warning("🟡 **模拟数据模式**：数据库为空，当前展示可复现模拟数据。"
               "运行「真实 Agent」页或「数据管理」页一键播种真实数据后，本页将自动切换为真实数据。")

# ---------- 系统状态横幅（跨页联动汇总）----------
sim = sim_get()
status_parts = []
if sim["abnormal_agent"]:
    status_parts.append(f"🔴 {sim['abnormal_agent']} 异常（拓扑页标记）")
if sim["quota_breach"]:
    status_parts.append("💰 成本配额熔断（成本核算页触发）")
if sim["rolled_back"]:
    status_parts.append("↩️ 已回滚到 v1.0（灰度发布页）")
elif sim["canary_stage"] > 0:
    canary_names = ["", "10% 小流量", "50% 半量", "100% 全量"]
    status_parts.append(f"🚦 灰度放量中：{canary_names[sim['canary_stage']]}")
if sim["release_decision"]:
    status_parts.append(f"🎯 发布决策：{sim['release_decision']}（版本对比页）")

if status_parts:
    has_critical = sim["abnormal_agent"] or sim["quota_breach"] or sim["rolled_back"]
    if has_critical:
        st.error("🚨 **系统状态异常** —— " + "　|　".join(status_parts)
                 + "　（点击左侧对应页面处理）", icon="🚨")
    else:
        st.info("ℹ️ **系统状态** —— " + "　|　".join(status_parts), icon="ℹ️")
else:
    st.success("✅ **系统状态：全部正常** —— 拓扑异常 / 配额熔断 / 发布决策 / 灰度进度跨页实时同步",
               icon="✅")

# ---------- 模式开关 ----------
live = st.toggle("⚡ Live 实时模式（每 1 秒模拟新调用流入）", value=False)
sim["live_on"] = live

# ============================================================
# Live 实时模式：fragment 每 1 秒自动重跑，数据滚动更新
# ============================================================
if live:
    from streamlit import fragment

    @fragment(run_every="1s")
    def live_panel():
        # 滚动窗口初始化：用静态数据尾部做基线
        if "live_window" not in st.session_state:
            st.session_state.live_window = df.tail(80).copy()

        # 追加一批新 Trace（模拟线上实时流入）
        now = datetime.now()
        batch_size = random.randint(5, 10)
        new_rows = []
        for i in range(batch_size):
            agent = random.choice(AGENTS)
            status = random.choices(["success", "error"], weights=[0.88, 0.12])[0]
            tokens = random.randint(3000, 12000)
            latency_ms = random.randint(400, 5000)
            new_rows.append({
                "trace_id": f"rt-{now:%H%M%S}-{i:02d}",
                "agent": agent,
                "time": now,
                "date": now.strftime("%m-%d"),
                "status": status,
                "tokens": tokens,
                "latency_ms": latency_ms,
                "cost": round(tokens / 1e6 * 7.2 * 12, 4),
                "n_steps": random.randint(3, 8),
            })
        batch = pd.DataFrame(new_rows)
        st.session_state.live_window = pd.concat(
            [st.session_state.live_window, batch], ignore_index=True
        ).tail(300)  # 滚动窗口：只保留最近 300 条

        win = st.session_state.live_window

        # 4 个 KPI（实时窗口口径）
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("窗口调用量", f"{len(win):,}")
        c2.metric("Token 消耗", f"{win['tokens'].sum() / 1e6:.2f}M")
        c3.metric("窗口成本", f"¥{win['cost'].sum():.4f}")
        c4.metric("平均延迟", f"{win['latency_ms'].mean() / 1000:.2f}s")

        # 实时趋势：按每 10 条分桶，形成滚动折线
        st.subheader("实时调用趋势（滚动窗口 · 每桶 10 条）")
        trend = win.copy()
        trend["seq"] = range(len(trend))
        trend["bucket"] = trend["seq"] // 10
        trend_agg = trend.groupby("bucket").size().reset_index(name="count")
        fig = px.line(trend_agg, x="bucket", y="count",
                      color_discrete_sequence=["#378ADD"])
        fig.update_layout(
            height=280,
            margin=dict(t=10, b=10, l=10, r=10),
            xaxis_title="时间桶（新→旧）",
            yaxis_title="调用量",
        )
        st.plotly_chart(fig, width="stretch")

        # 最新 Trace
        st.subheader("最新链路 Trace（实时）")
        st.dataframe(
            win.tail(15)[["time", "trace_id", "agent", "status", "n_steps", "tokens", "latency_ms", "cost"]]
            .rename(columns={"time": "时间", "trace_id": "Trace ID", "agent": "Agent",
                             "status": "状态", "n_steps": "步骤数", "tokens": "Token",
                             "latency_ms": "耗时(ms)", "cost": "成本(¥)"}),
            width="stretch",
            hide_index=True,
        )
        st.caption(f"⏱️ 最近刷新：{now:%H:%M:%S} · 窗口容量 300 条")

    live_panel()

# ============================================================
# 静态模式：近 14 天聚合视图（原有内容）
# ============================================================
else:
    # 4 个 KPI
    today = df["date"].max()
    today_df = df[df["date"] == today]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("今日调用量", f"{len(today_df):,}")
    c2.metric("Token 消耗", f"{today_df['tokens'].sum() / 1e6:.2f}M")
    c3.metric("今日成本", f"¥{today_df['cost'].sum():.4f}")
    c4.metric("平均延迟", f"{df['latency_ms'].mean() / 1000:.2f}s")

    # 真实 Trace 聚合（仅模拟模式下若存在真实落库 Trace，单独高亮）
    if mode == "mock":
        REAL_AGENTS = {"研发管家 · 多Agent编排", "知源 · RAG问答", "通用问答 · DeepSeek"}
        real_df = df[df["agent"].isin(REAL_AGENTS)]
        if not real_df.empty:
            st.subheader("今日真实调用（来自 agent_ops.db）")
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("真实调用量", f"{len(real_df):,}")
            r2.metric("真实 Token", f"{real_df['tokens'].sum():,}")
            r3.metric("真实成本", f"¥{real_df['cost'].sum():.3f}")
            r4.metric("平均延迟", f"{real_df['latency_ms'].mean() / 1000:.2f}s")

    # 近 14 天调用量趋势
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

    # 最新 Trace
    st.subheader("最新链路 Trace")
    st.dataframe(
        df.head(20)[["time", "trace_id", "agent", "status", "n_steps", "tokens", "latency_ms", "cost"]]
        .rename(columns={"time": "时间", "trace_id": "Trace ID", "agent": "Agent",
                         "status": "状态", "n_steps": "步骤数", "tokens": "Token",
                         "latency_ms": "耗时(ms)", "cost": "成本(¥)"}),
        width="stretch",
        hide_index=True,
    )

    st.divider()
    st.caption("开启上方 ⚡ Live 实时模式，体验面板实时滚动效果")
