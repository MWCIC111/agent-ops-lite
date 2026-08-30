# 7_Agent拓扑.py —— 研发管家 · 真实多 Agent 架构图
# 展示简历核心项目「研发管家」的 LangGraph 式集中式架构：
#   Orchestrator（集中式编排）+ 共享 State + 4 个垂直 Agent
#   （抗原设计 / 方案规划 / 故障诊断 / 资料整理），末段串联
#   置信度融合 · 三层幻觉抑制流水线。
# 面试叙事："这就是我设计的 4 垂直 Agent + Orchestrator 集中式多 Agent 协作体系——
#            所有 Agent 读写同一份共享 State，Orchestrator 做任务编排，
#            任何一个环节出问题都能从拓扑图上定位到具体 Agent。"
# 联动设计：在本页标记某个 Agent 异常后，状态写入 shared_state 全局共享，
#           告警页 / 首页 / 工具分析页会同步感知——模拟真实生产中所有面板读同一个后端。

import random

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import show_clock, page_visit
from op_log import log_operation

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_state import init as sim_init, get as sim_get

sim_init()  # 初始化全局共享状态

st.set_page_config(page_title="Agent 拓扑 · 研发管家架构", page_icon="🕸️", layout="wide")

st.title("🕸️ Agent 拓扑 · 研发管家（LangGraph 式集中式架构）")
show_clock()
page_visit("Agent拓扑")
st.caption("Orchestrator 集中式编排 + 共享 State + 4 个垂直 Agent（抗原设计 / 方案规划 / 故障诊断 / 资料整理）——"
           "节点大小 = 调用量，颜色 = 成功率，悬停查看职责。")

# ---------- 1. 真实拓扑定义（对应简历核心项目「研发管家」）----------
# (x, y, 调用量, 成功率, 职责, 平均耗时ms)
NODES = {
    "共享State": (0, 0, 2400, 0.99, "集中式共享状态（TypedDict）：各 Agent 读写同一份上下文，保证一致性与可追溯", 120),
    "Orchestrator": (0, 2.6, 1800, 0.95, "集中式编排：任务拆解、分发、调度 4 个垂直 Agent，汇总最终结果", 360),
    "抗原设计 Agent": (-2.6, 1.2, 720, 0.93, "垂直Agent①：抗原/表位设计、免疫原性权衡、设计风险点", 540),
    "方案规划 Agent": (2.6, 1.2, 880, 0.94, "垂直Agent②：研发/实验方案拆解、排期、依赖与验收口径", 420),
    "故障诊断 Agent": (-2.6, -1.2, 760, 0.91, "垂直Agent③：异常/瓶颈/风险根因定位与对策", 380),
    "资料整理 Agent": (2.6, -1.2, 1040, 0.97, "垂直Agent④：汇总各 Agent 结论，整理结构化交付报告", 300),
    "置信度融合·幻觉抑制": (0, -2.8, 1260, 0.96, "流水线末段：三层幻觉抑制(工具/LLM/输出) + 置信度融合(Milvus相似度+logprob+规则)", 600),
}
# 调用关系：Orchestrator 与共享State 双向；共享State 与 4 垂直 Agent 双向；
# 两个下游 Agent 汇入置信度融合。
EDGES = [
    ("Orchestrator", "共享State"),
    ("Orchestrator", "抗原设计 Agent"),
    ("Orchestrator", "方案规划 Agent"),
    ("Orchestrator", "故障诊断 Agent"),
    ("Orchestrator", "资料整理 Agent"),
    ("共享State", "抗原设计 Agent"),
    ("共享State", "方案规划 Agent"),
    ("共享State", "故障诊断 Agent"),
    ("共享State", "资料整理 Agent"),
    ("资料整理 Agent", "置信度融合·幻觉抑制"),
    ("故障诊断 Agent", "置信度融合·幻觉抑制"),
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
    st.metric("Agent 数量", f"{len(NODES)}", "1 编排 + 1 共享State + 4 垂直")

# 联动提示条
ab = sim_get()["abnormal_agent"]
if ab:
    st.warning(f"🔗 **已联动**：{ab} 异常已写入全局状态——前往「告警与异常」页可看到对应告警，"
               f"「总览」页顶部横幅同步提示。", icon="🔗")
else:
    st.caption("🔗 联动提示：标记异常后，告警页 / 首页 / 工具分析页会同步感知（模拟共享后端）。")

# ---------- 3. 拓扑图 ----------
st.subheader("🗺️ 调用关系拓扑（研发管家）")
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
    height=480,
    xaxis=dict(visible=False, range=[-3.4, 3.4]),
    yaxis=dict(visible=False, range=[-3.6, 3.6]),
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
st.subheader("📋 Agent 节点明细（研发管家）")
rows = []
ab = sim_get()["abnormal_agent"]
for name, (x, y, calls, succ, role, lat) in NODES.items():
    nx, ny, ncalls, nsucc, nrole, nlat = node_metrics(name)
    status = "🔴 异常" if ab == name else ("🟠 需关注" if nsucc < 0.85 else "🟢 健康")
    rows.append({"Agent": name, "职责": nrole, "调用量": ncalls,
                 "成功率": f"{nsucc:.1%}", "平均耗时": f"{nlat}ms", "状态": status})
st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

# ---------- 4.5 真实调用统计（来自 agent_ops.db）----------
from demo_data import load_traces
from collections import Counter

_real_traces, _rmode = load_traces()
if _rmode == "real":
    st.subheader("📊 真实调用统计（来自 agent_ops.db）")
    cnt = Counter(t.agent for t in _real_traces)
    stats_rows = [
        {
            "Agent": a,
            "调用次数": c,
            "成功率": f"{(sum(1 for t in _real_traces if t.agent == a and t.status == 'success') / c):.1%}",
        }
        for a, c in cnt.most_common()
    ]
    st.dataframe(pd.DataFrame(stats_rows), width="stretch", hide_index=True)
    st.caption(f"共 {len(_real_traces)} 条真实 Trace 已落库；与上方架构图的 Agent 一一对应。")
else:
    st.caption("💡 运行「真实 Agent」或「数据管理」页播种真实数据后，此处展示各 Agent 的真实调用统计。")

# ---------- 5. 置信度融合 · 三层幻觉抑制 流水线 ----------
st.subheader("🛡️ 置信度融合 · 三层幻觉抑制流水线")
st.caption("研发管家在末段对所有 Agent 结论做质量闸门：三层抑制 + 置信度融合，高风险结论进入人工审核后才回写知识库。")

pipe = [
    ("工具层抑制", "事实一致性：检索/工具输出与依据比对，拦截无依据断言"),
    ("LLM层抑制", "逻辑自洽：logprob 置信度 + 内部一致性，低置信片段标记"),
    ("输出层抑制", "与检索依据对齐：输出不得偏离依据，否则降级/拦截"),
    ("置信度融合", "Milvus相似度 + LLM logprob + 业务规则 三者加权融合"),
    ("人工审核", "高风险结论进审核队列，通过才放行（人工审核后回写）"),
    ("Milvus回写", "审核通过的 Q&A 回写向量库，持续增强检索质量"),
]
px_vals = list(range(len(pipe)))
pfig = go.Figure()
pfig.add_trace(go.Scatter(
    x=px_vals, y=[1] * len(pipe),
    mode="lines+markers+text",
    marker=dict(size=26, color="#4f8bf9", line=dict(color="#ffffff", width=1.5)),
    text=[p[0] for p in pipe],
    textposition="bottom center",
    textfont=dict(size=11),
    line=dict(color="#8b93a7", width=2),
    hovertext=[f"<b>{p[0]}</b><br>{p[1]}" for p in pipe],
    hovertemplate="%{hovertext}<extra></extra>",
))
pfig.update_layout(
    height=180,
    xaxis=dict(visible=False, range=[-0.5, len(pipe) - 0.5]),
    yaxis=dict(visible=False, range=[0.6, 1.4]),
    margin=dict(t=10, b=40, l=10, r=10),
)
st.plotly_chart(pfig, width="stretch")
with st.expander("查看各阶段说明"):
    for i, (nm, desc) in enumerate(pipe, 1):
        st.markdown(f"{i}. **{nm}**：{desc}")

# ---------- 6. 架构说明 ----------
st.subheader("💡 架构设计（对应简历「研发管家」）")
st.markdown(
    "**集中式多 Agent（Orchestrator + 共享 State + 4 垂直 Agent）**：\n"
    "- **从 Dify 迁移而来**，自建 LangGraph 式状态图，集中式调度而非分散式对话。\n"
    "- **共享 State（TypedDict）**：所有 Agent 读写同一份上下文，保证一致性与可追溯。\n"
    "- **4 个垂直 Agent 各司其职**：抗原设计 / 方案规划 / 故障诊断 / 资料整理。\n"
    "- **自研工具抽象层（非 MCP）**：本地 Python + 内部 HTTP + JSON Schema 描述工具，统一调用入口。\n"
    "- **质量闸门**：三层幻觉抑制（工具层 → LLM 层 → 输出层）+ 置信度融合（Milvus 相似度 + logprob + 业务规则）；"
    "高风险结论经人工审核后回写 Milvus，持续增强检索。\n\n"
    "任一 Agent 成功率下降，拓扑图上立即变色，配合 Trace ID 可分钟级定位到具体环节。"
)

st.divider()
st.caption("💡 演示说明：节点指标与调用关系为演示数据（量级参考真实项目）；架构结构与生产「研发管家」一致。"
           "真实 Agent 页「研发管家 · 研发问答」场景可现场跑通上述多步编排并落 Trace。")
