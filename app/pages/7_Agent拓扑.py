# 7_Agent拓扑.py —— 多 Agent 拓扑图
# 展示 Orchestrator + 4 个垂直 Agent 的调用关系网络：节点大小=调用量、颜色=成功率、hover=职责。
# 面试叙事："这就是我设计的 4 垂直 Agent + Orchestrator 多 Agent 协作体系——
#            一个 Orchestrator 做任务编排，四个垂直 Agent 各司其职，任何一个环节
#            出问题都能从拓扑图上定位到具体 Agent。"
# 联动设计：在本页标记某个 Agent 异常后，状态写入 shared_state 全局共享，
#           告警页 / 首页 / 工具分析页会同步感知——模拟真实生产中所有面板读同一个后端。

import random

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_state import init as sim_init, get as sim_get

sim_init()  # 初始化全局共享状态

st.set_page_config(page_title="Agent 拓扑 · 多 Agent 架构", page_icon="🕸️", layout="wide")

st.title("🕸️ Agent 拓扑 · 多 Agent 架构")
st.caption("Orchestrator 统一编排，4 个垂直 Agent 各司其职——节点大小 = 调用量，颜色 = 成功率，悬停查看职责。")

# ---------- 1. 拓扑定义 ----------
# (x, y, 调用量, 成功率, 职责, 平均耗时ms)
NODES = {
    "Orchestrator": (0, 0, 1600, 0.86, "任务编排、分发与全局调度", 380),
    "规划 Agent": (-2.2, 2.2, 620, 0.92, "任务拆解、生成执行计划", 210),
    "检索 Agent": (2.2, 2.2, 980, 0.95, "知识库混合检索（BM25+向量）", 260),
    "推理 Agent": (2.2, -2.2, 1150, 0.88, "模型推理、生成答复内容", 540),
    "校验 Agent": (-2.2, -2.2, 760, 0.97, "防幻觉校验、三层质检", 190),
}
# 调用关系：Orchestrator 与四个垂直 Agent 双向协作
EDGES = [
    ("Orchestrator", "规划 Agent"),
    ("Orchestrator", "检索 Agent"),
    ("Orchestrator", "推理 Agent"),
    ("Orchestrator", "校验 Agent"),
    ("规划 Agent", "检索 Agent"),
    ("推理 Agent", "校验 Agent"),
]


def node_metrics(name):
    """节点指标：异常模式覆盖为低成功率。"""
    sim = sim_get()
    x, y, calls, succ, role, lat = NODES[name]
    if sim["abnormal_agent"] == name:
        succ = 0.62  # 模拟该 Agent 故障
    return x, y, calls, succ, role, lat


# ---------- 2. 控制台 ----------
st.subheader("🎛️ 控制台")
cc1, cc2 = st.columns([2, 1])
with cc1:
    options = ["无（全部正常）"] + list(NODES.keys())
    choice = st.selectbox(
        "模拟 Agent 异常（将同步到告警页 / 首页 / 工具分析页）", options, index=0,
        help="真实生产中，拓扑页的异常标记来自监控告警联动；这里手动模拟，用于演示全系统联动。",
    )
    sim_get()["abnormal_agent"] = None if choice == "无（全部正常）" else choice
with cc2:
    st.metric("Agent 数量", f"{len(NODES)}", "1 编排 + 4 垂直")

# 联动提示条
ab = sim_get()["abnormal_agent"]
if ab:
    st.warning(f"🔗 **已联动**：{ab} 异常已写入全局状态——前往「告警与异常」页可看到对应告警，"
               f"「总览」页顶部横幅同步提示。", icon="🔗")
else:
    st.caption("🔗 联动提示：标记异常后，告警页 / 首页 / 工具分析页会同步感知（模拟共享后端）。")

# ---------- 3. 拓扑图 ----------
st.subheader("🗺️ 调用关系拓扑")
fig = go.Figure()

# 画边（细线，带箭头语义的浅色连线）
for src, dst in EDGES:
    sx, sy = NODES[src][0], NODES[src][1]
    dx, dy = NODES[dst][0], NODES[dst][1]
    fig.add_trace(go.Scatter(
        x=[sx, dx], y=[sy, dy],
        mode="lines",
        line=dict(color="#8b93a7", width=1.5),
        hoverinfo="skip",
        showlegend=False,
    ))

# 画节点（大小=调用量，颜色=成功率，text=名称）
xs, ys, sizes, colors, labels, hover_texts = [], [], [], [], [], []
ab = sim_get()["abnormal_agent"]
for name, (x, y, calls, succ, role, lat) in NODES.items():
    nx, ny, ncalls, nsucc, nrole, nlat = node_metrics(name)
    xs.append(nx); ys.append(ny)
    sizes.append(ncalls / 45)
    # 颜色：异常红 / 成功率低橙 / 正常蓝
    if ab == name:
        colors.append("#ef553b")
    elif nsucc < 0.85:
        colors.append("#f0a94d")
    else:
        colors.append("#4f8bf9")
    labels.append(name)
    hover_texts.append(
        f"<b>{name}</b><br>职责：{nrole}<br>调用量：{ncalls:,}<br>成功率：{nsucc:.1%}<br>平均耗时：{nlat}ms"
    )

fig.add_trace(go.Scatter(
    x=xs, y=ys,
    mode="markers+text",
    marker=dict(size=sizes, color=colors, opacity=0.85,
                line=dict(color="#ffffff", width=1.5)),
    text=labels,
    textposition="top center",
    customdata=hover_texts,
    hovertemplate="%{customdata}<extra></extra>",
))

fig.update_layout(
    height=460,
    xaxis=dict(visible=False, range=[-3.2, 3.2]),
    yaxis=dict(visible=False, range=[-3.2, 3.2]),
    margin=dict(t=10, b=10, l=10, r=10),
)
st.plotly_chart(fig, width="stretch")

# 图例说明
lg1, lg2, lg3, lg4 = st.columns(4)
lg1.caption("🟦 节点大小 = 调用量")
lg2.caption("🟦 蓝色 = 正常")
lg3.caption("🟧 橙色 = 成功率偏低")
lg4.caption("🟥 红色 = 异常/故障")

# ---------- 4. 节点明细表 ----------
st.subheader("📋 Agent 节点明细")
rows = []
ab = sim_get()["abnormal_agent"]
for name, (x, y, calls, succ, role, lat) in NODES.items():
    nx, ny, ncalls, nsucc, nrole, nlat = node_metrics(name)
    status = "🔴 异常" if ab == name else              ("🟠 需关注" if nsucc < 0.85 else "🟢 健康")
    rows.append({"Agent": name, "职责": nrole, "调用量": ncalls,
                 "成功率": f"{nsucc:.1%}", "平均耗时": f"{nlat}ms", "状态": status})
st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

# ---------- 5. 架构说明 ----------
st.subheader("💡 架构设计")
st.markdown(
    "**Orchestrator + 垂直 Agent 模式**：Orchestrator 负责任务编排与分发，"
    "4 个垂直 Agent 各司其职（规划 / 检索 / 推理 / 校验）。"
    "任一 Agent 成功率下降，拓扑图上立即变色，配合 Trace ID 可分钟级定位到具体环节。"
)

st.divider()
st.caption("💡 演示说明：节点与调用关系为模拟数据。真实场景中，节点数据来自各 Agent 的监控聚合，"
           "调用关系即多 Agent 协作拓扑——结构与生产一致。")
