# 5_版本对比.py —— A/B 测试对比视图
# 新版本上线前，先跑 A/B 对比：成功率 / 延迟 / 成本 三个维度，用数据决定是否全量发布。
# 面试叙事："上线新版本前，我先跑 A/B 对比决定是否全量发布——这就是灰度发布的数据依据。"
# 联动设计：发布结论写入全局共享状态，可一键带入「灰度发布」页执行放量——
#           版本对比（发不发）→ 灰度发布（怎么发）→ 告警监控（健康吗）→ 自动回滚（出事怎么办）。

import random

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_state import init as sim_init, get as sim_get

sim_init()

st.set_page_config(page_title="版本对比 · A/B 测试", page_icon="🔬", layout="wide")

st.title("🔬 版本对比 · A/B 测试")
st.caption("新版本上线前，先跑 A/B 对比：成功率 / 延迟 / 成本 三个维度，用数据决定是否全量发布。")

# ---------- 1. 数据来源：真实基线优先 ----------
# 真实场景中：从 A/B 流量分组各自采集 n 条 Trace，即可复用下面的对比逻辑。
# 此处 v1.0 基线优先取真实 Trace 聚合（真实数据模式），否则用可复现模拟基线。
from demo_data import load_traces, real_baseline

_traces, _mode = load_traces()
_base = real_baseline(_traces) if _mode == "real" else None


@st.cache_data
def gen_traces(version: str, seed: int, n: int = 1000) -> pd.DataFrame:
    """生成某版本 n 条 Trace 的关键指标（成功率 / 延迟 / Token 消耗）。"""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        if version == "v1.0":                # 对照组：基线表现
            success = rng.random() < 0.84    # 成功率 84%
            latency = rng.gauss(2.4, 0.6)    # 平均延迟 2.4s
            tokens = rng.gauss(8200, 1200)   # 平均 8.2K tokens
        else:                                # v1.1 实验组：优化后的表现
            success = rng.random() < 0.87    # 成功率 87%
            latency = rng.gauss(1.9, 0.5)    # 平均延迟 1.9s
            tokens = rng.gauss(7500, 1100)   # 平均 7.5K tokens
        rows.append({
            "version": version,
            "success": success,
            "latency_s": round(max(latency, 0.1), 2),
            "tokens": int(max(tokens, 100)),
        })
    return pd.DataFrame(rows)


def _real_df(base_metrics, improved: bool, n: int = 1000) -> pd.DataFrame:
    """用真实基线指标生成 n 条 Trace（improved=True 表示 v1.1 假设优化）。"""
    rng = random.Random(99)
    sr = base_metrics["success_rate"]
    lat = base_metrics["avg_latency_s"]
    tok = base_metrics["avg_tokens"]
    if improved:
        sr = min(sr + 0.03, 0.99)
        lat *= 0.8
        tok *= 0.9
    rows = []
    for _ in range(n):
        rows.append({
            "version": "v1.1" if improved else "v1.0",
            "success": rng.random() < sr,
            "latency_s": round(max(lat * rng.gauss(1, 0.15), 0.1), 2),
            "tokens": int(max(tok * rng.gauss(1, 0.1), 100)),
        })
    return pd.DataFrame(rows)


if _base:
    df_v1 = _real_df(_base, improved=False)
    df_v11 = _real_df(_base, improved=True)
    TOOL_SUCCESS = {
        "v1.0": _base["tool_success_rate"],
        "v1.1": min(_base["tool_success_rate"] + 0.03, 0.99),
    }
    st.info("🟢 **真实数据模式**：v1.0 基线来自真实 Trace 聚合（成功率 / 延迟 / Token / 工具成功率）；"
            "v1.1 为在其基础上的假设优化，用于演示 A/B 发布决策逻辑。")
else:
    df_v1 = gen_traces("v1.0", seed=10)
    df_v11 = gen_traces("v1.1", seed=11)
    # 工具成功率（演示用固定值；真实场景由工具调用记录聚合得到）
    TOOL_SUCCESS = {"v1.0": 0.91, "v1.1": 0.94}

# ---------- 2. 核心指标对比卡 ----------
st.subheader("📊 核心指标对比")
c1, c2, c3, c4 = st.columns(4)

# delta_color: normal=正值绿 / inverse=负值绿（延迟、成本下降是好事）
c1.metric("任务成功率", f"{df_v11.success.mean():.1%}",
          f"{df_v11.success.mean() - df_v1.success.mean():+.1%}", delta_color="normal")
c2.metric("平均延迟", f"{df_v11.latency_s.mean():.2f}s",
          f"{df_v11.latency_s.mean() - df_v1.latency_s.mean():+.2f}s", delta_color="inverse")
c3.metric("平均 Token/次", f"{df_v11.tokens.mean():,.0f}",
          f"{df_v11.tokens.mean() - df_v1.tokens.mean():+,.0f}", delta_color="inverse")
c4.metric("工具成功率", f"{TOOL_SUCCESS['v1.1']:.1%}",
          f"{TOOL_SUCCESS['v1.1'] - TOOL_SUCCESS['v1.0']:+.1%}", delta_color="normal")

# ---------- 3. 延迟分布对比图 ----------
st.subheader("⏱️ 延迟分布对比")
fig = go.Figure()
fig.add_trace(go.Histogram(x=df_v1.latency_s, name="v1.0 对照组",
                           opacity=0.6, marker_color="#8b93a7"))
fig.add_trace(go.Histogram(x=df_v11.latency_s, name="v1.1 实验组",
                           opacity=0.6, marker_color="#4f8bf9"))
# 两条均值虚线：一眼看出分布整体位移
fig.add_vline(x=df_v1.latency_s.mean(), line_dash="dash", line_color="#8b93a7",
              annotation_text=f"v1.0 均值 {df_v1.latency_s.mean():.2f}s")
fig.add_vline(x=df_v11.latency_s.mean(), line_dash="dash", line_color="#4f8bf9",
              annotation_text=f"v1.1 均值 {df_v11.latency_s.mean():.2f}s")
fig.update_layout(barmode="overlay", xaxis_title="延迟（秒）", yaxis_title="Trace 数",
                  height=380, legend=dict(orientation="h", yanchor="bottom", y=1.02))
st.plotly_chart(fig, width="stretch")

# ---------- 4. 发布结论卡 ----------
st.subheader("🎯 发布结论")
improvements = []
if df_v11.success.mean() > df_v1.success.mean():
    improvements.append(f"成功率 +{(df_v11.success.mean() - df_v1.success.mean()) * 100:.1f}%")
if df_v11.latency_s.mean() < df_v1.latency_s.mean():
    improvements.append(f"延迟 -{abs(df_v11.latency_s.mean() - df_v1.latency_s.mean()) / df_v1.latency_s.mean() * 100:.0f}%")
if df_v11.tokens.mean() < df_v1.tokens.mean():
    improvements.append(f"成本 -{abs(df_v11.tokens.mean() - df_v1.tokens.mean()) / df_v1.tokens.mean() * 100:.0f}%")

# 判定结论并写入全局状态（灰度发布页会读取）
if len(improvements) >= 2:
    decision = "全量发布"
    st.success(f"**建议：全量发布 v1.1** 🚀 —— 相比 v1.0，{'、'.join(improvements)}，核心指标全面占优。")
elif len(improvements) >= 1:
    decision = "灰度验证"
    st.info(f"**建议：小流量灰度验证** —— 相比 v1.0，{'、'.join(improvements)}，但仍有指标未提升，建议灰度观察后再全量。")
else:
    decision = "暂缓发布"
    st.error("**建议：暂缓发布** —— v1.1 未见明显优势，需定位根因后再评估。")

sim_get()["release_decision"] = decision

# ---------- 5. 联动：将结论带入灰度发布页 ----------
st.subheader("🔗 联动操作")
lc1, lc2 = st.columns([2, 1])
with lc1:
    st.caption(
        f"当前结论：**{decision}**。点击右侧按钮，把结论写入全局状态，"
        f"「灰度发布」页将按此结论执行放量。"
    )
with lc2:
    if st.button("➡️ 将结论带入灰度发布页", width="stretch"):
        st.session_state["carry_decision"] = True
        st.success("已写入全局状态 ✅ 前往「灰度发布」页查看联动效果。")

st.divider()
st.caption("💡 演示说明：此处为模拟数据（固定随机种子可复现）。真实场景中，从 A/B 流量分组采集两端 Trace，"
           "即可用同一套对比逻辑输出发布结论——数据结构和对比逻辑都是通用的。")
