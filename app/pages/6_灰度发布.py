# 6_灰度发布.py —— 灰度放量视图
# 新版本发布不是一把梭：10% → 50% → 100% 渐进放量，每阶段监控健康度，异常自动回滚。
# 面试叙事："新版本上线前先跑 A/B（版本对比页）决定发不发，发布时渐进放量、
#            分阶段监控、异常自动回滚——这就是我在公司 12 次灰度零事故的工程方法。"

import random

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="灰度发布 · 渐进放量", page_icon="🚦", layout="wide")

st.title("🚦 灰度发布 · 渐进放量")
st.caption("新版本发布不是一把梭：10% → 50% → 100% 渐进放量，每个阶段监控健康度，异常自动回滚。")

# ---------- 0. 会话状态 ----------
if "stage_idx" not in st.session_state:
    st.session_state.stage_idx = 0
if "abnormal" not in st.session_state:
    st.session_state.abnormal = False
if "rolled_back" not in st.session_state:
    st.session_state.rolled_back = False

# ---------- 1. 放量阶段定义（模拟数据） ----------
# 真实场景中：每阶段从线上监控聚合该阶段的实际指标，健康阈值由 SLO 决定。
STAGES = [
    {"name": "10% 小流量", "pct": 0.10, "success": 0.873, "err_rate": 0.013, "latency": 1.90},
    {"name": "50% 半量", "pct": 0.50, "success": 0.871, "err_rate": 0.014, "latency": 1.95},
    {"name": "100% 全量", "pct": 1.00, "success": 0.869, "err_rate": 0.016, "latency": 2.02},
]
ABNORMAL = {"success": 0.762, "err_rate": 0.091, "latency": 3.42}
HEALTH_OK = {"err_rate": 0.05, "success": 0.80}  # 健康阈值：错误率<5% 且 成功率>80%


def current_metrics(stage):
    """当前阶段实际指标：异常模式覆盖为异常值。"""
    if st.session_state.abnormal:
        return ABNORMAL
    return stage


def is_healthy(m):
    return m["err_rate"] < HEALTH_OK["err_rate"] and m["success"] > HEALTH_OK["success"]


# ---------- 2. 放量控制台 ----------
st.subheader("🎛️ 放量控制台")
c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    st.session_state.abnormal = st.toggle(
        "模拟异常：本阶段错误率飙升", value=st.session_state.abnormal)
with c2:
    if st.button("➡️ 推进下一阶段", disabled=st.session_state.stage_idx >= 2,
                 width="stretch"):
        st.session_state.stage_idx = min(st.session_state.stage_idx + 1, 2)
        st.session_state.rolled_back = False
with c3:
    if st.button("↩️ 模拟回滚并重置", width="stretch"):
        st.session_state.stage_idx = 0
        st.session_state.abnormal = False
        st.session_state.rolled_back = True

# ---------- 3. 放量进度 ----------
stage = STAGES[st.session_state.stage_idx]
m = current_metrics(stage)
healthy = is_healthy(m)

st.progress(stage["pct"], text=f"当前放量：{stage['name']}（{stage['pct']:.0%} 流量）")

# ---------- 4. 当前阶段指标卡 ----------
mc1, mc2, mc3 = st.columns(3)
mc1.metric("任务成功率", f"{m['success']:.1%}")
mc2.metric("错误率", f"{m['err_rate']:.1%}")
mc3.metric("P95 延迟", f"{m['latency']:.2f}s")

# ---------- 5. 健康度判定 ----------
if st.session_state.rolled_back:
    st.warning("↩️ **已回滚到 v1.0**：异常阶段流量已切回旧版本，线上服务恢复稳定。"
               "（生产环境动作：回滚 + 告警通知 + 定位根因）")
elif not healthy:
    st.error(f"🚨 **检测到异常，触发自动回滚**：错误率 {m['err_rate']:.1%} 超过阈值 "
             f"{HEALTH_OK['err_rate']:.0%}，系统已自动将流量切回 v1.0。")
elif st.session_state.stage_idx >= 2:
    st.success("🎉 **全量发布完成**：三个阶段全部健康，v1.1 已 100% 上线。")
else:
    st.success(f"✅ **阶段健康**：当前 {stage['name']} 指标正常，可推进下一阶段。")

# ---------- 6. 放量历史 ----------
st.subheader("📜 放量历史")
history = []
for i, s in enumerate(STAGES[: st.session_state.stage_idx + 1]):
    row = {"阶段": s["name"], "流量": f"{s['pct']:.0%}", "成功率": f"{s['success']:.1%}",
           "错误率": f"{s['err_rate']:.1%}", "延迟": f"{s['latency']:.2f}s"}
    if i == st.session_state.stage_idx and st.session_state.abnormal:
        row.update({"成功率": f"{ABNORMAL['success']:.1%}", "错误率": f"{ABNORMAL['err_rate']:.1%}",
                    "延迟": f"{ABNORMAL['latency']:.2f}s", "状态": "🔴 异常回滚"})
    else:
        row["状态"] = "🟢 健康"
    history.append(row)
st.dataframe(pd.DataFrame(history), width="stretch", hide_index=True)

# ---------- 7. 错误率 vs 健康阈值图 ----------
st.subheader("📊 各阶段错误率 vs 健康阈值")
fig = go.Figure()
fig.add_trace(go.Bar(x=[s["name"] for s in STAGES], y=[s["err_rate"] for s in STAGES],
                     name="正常错误率", marker_color="#4f8bf9"))
if st.session_state.abnormal:
    fig.add_trace(go.Bar(x=[stage["name"]], y=[ABNORMAL["err_rate"]],
                         name="异常错误率", marker_color="#ef553b"))
fig.add_hline(y=HEALTH_OK["err_rate"], line_dash="dash", line_color="#ff4b4b",
              annotation_text=f"健康阈值 {HEALTH_OK['err_rate']:.0%}")
fig.update_layout(barmode="group", xaxis_title="放量阶段", yaxis_title="错误率",
                  height=340, yaxis_tickformat=".1%",
                  legend=dict(orientation="h", yanchor="bottom", y=1.02))
st.plotly_chart(fig, width="stretch")

st.divider()
st.caption("💡 演示说明：此处为模拟数据。真实场景中，每个阶段的指标来自线上监控聚合，"
           "健康阈值由 SLO 决定，异常时自动执行回滚 + 告警——交互逻辑与生产一致。")
