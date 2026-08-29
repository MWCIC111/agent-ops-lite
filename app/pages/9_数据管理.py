"""9_数据管理.py — 真实数据管理

把所有观测页面统一到「真实数据」后的管理入口：
  - 查看 agent_ops.db 中真实 Trace 的统计（数量 / 各 Agent / 成本 / 最近时间）
  - 一键播种真实数据（调用 DeepSeek + 华佗百科 RAG，批量落库）
  - 清空真实数据（重新播种前用）

所有页面（总览 / 链路追踪 / 工具分析 / 成本核算 / 告警 / 版本对比 / 灰度 / 拓扑）
现在统一读取 agent_ops.db 的真实 Trace；数据库为空时回退模拟数据并打标识。
"""
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime

import pandas as pd
import streamlit as st

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))

from agent_ops import SQLiteStore  # noqa: E402

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")
SEED_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "seed_real_data.py")
SEED_LOG = os.path.join(_REPO_ROOT, "seed.log")

st.set_page_config(page_title="数据管理 · agent-ops-lite", layout="wide")
st.title("🗄️ 数据管理（真实数据源）")
st.caption("所有观测页面统一读取 agent_ops.db 的真实 Trace；数据库为空时回退模拟数据。")

# ---------- 1. 当前真实数据统计 ----------
traces = SQLiteStore(DB_PATH).load()
if traces:
    n = len(traces)
    succ = sum(1 for t in traces if t.status == "success")
    total_cost = sum(t.cost_usd for t in traces) * 7.2
    cnt = Counter(t.agent for t in traces)
    last = max(t.started_at for t in traces)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("真实 Trace 数", f"{n:,}")
    c2.metric("成功率", f"{succ / n:.1%}")
    c3.metric("累计成本", f"¥{total_cost:.2f}")
    c4.metric("最近调用", last.strftime("%m-%d %H:%M"))
    st.subheader("各 Agent 调用分布")
    rows = [
        {"Agent": a, "调用次数": c,
         "成功率": f"{(sum(1 for t in traces if t.agent == a and t.status == 'success') / c):.1%}"}
        for a, c in cnt.most_common()
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.success("🟢 数据库非空，所有观测页面当前展示真实数据。")
else:
    st.warning("🟡 数据库为空，所有观测页面当前展示可复现模拟数据。点击下方按钮播种真实数据。")

# ---------- 2. 一键播种真实数据 ----------
st.divider()
st.subheader("🌱 播种真实数据（真实 DeepSeek + 华佗百科 RAG）")
st.markdown(
    "批量跑真实 Agent 问答，每次调用都被 `@trace` 采集并落库，让所有面板都有真实数据可看。"
    "**研发管家为多步编排（每条约 6~7 次 LLM 调用），整体较慢，建议在后台运行。**"
)
col1, col2 = st.columns([1, 2])
with col1:
    rag_n = st.number_input("知源 RAG 条数", min_value=0, max_value=200, value=40)
    butler_n = st.number_input("研发管家条数", min_value=0, max_value=50, value=8)
    general_n = st.number_input("通用问答条数", min_value=0, max_value=200, value=15)
    fail_n = st.number_input("注入真实失败条数", min_value=0, max_value=20, value=3)
with col2:
    cmd = (f"python3 scripts/seed_real_data.py --rag {rag_n} --butler {butler_n} "
           f"--general {general_n} --failures {fail_n} --spread-days 14")
    st.code(f"cd /home/ubuntu/agent-ops-lite && nohup {cmd} > seed.log 2>&1 &",
            language="bash")
    st.caption("复制上面的命令到 OrcaTerm 执行；或点右侧按钮在服务器后台直接启动。")

run_col, _ = st.columns([1, 1])
if run_col.button("🚀 在服务器后台运行播种", type="primary"):
    try:
        subprocess.Popen(
            f"nohup {sys.executable} scripts/seed_real_data.py --rag {rag_n} "
            f"--butler {butler_n} --general {general_n} --failures {fail_n} "
            f"--spread-days 14 > seed.log 2>&1 &",
            shell=True, cwd=_REPO_ROOT,
        )
        st.success("已在后台启动播种（研发管家较慢，请耐心等待）。"
                   "播种期间请勿在「真实 Agent」页点运行，避免并发写库。刷新本页查看进度。")
    except Exception as e:  # noqa: BLE001
        st.error(f"启动失败：{e}")

if st.button("🔄 刷新统计"):
    st.rerun()

# 显示 seed.log 尾部的进度
if os.path.exists(SEED_LOG):
    with st.expander("查看 seed.log（播种进度）"):
        with open(SEED_LOG, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-30:]
        st.text("".join(lines))

# ---------- 3. 清空真实数据（危险操作） ----------
st.divider()
st.subheader("⚠️ 清空真实数据")
st.caption("仅在需要重新播种时使用。会删除 agent_ops.db 中所有真实 Trace（模拟兜底不受影响）。")
if st.checkbox("我确认要清空 agent_ops.db 中的所有真实 Trace"):
    if st.button("🗑️ 确认清空", type="primary"):
        SQLiteStore(DB_PATH).clear()
        st.success("已清空。刷新后所有页面将回退为模拟数据，可重新播种。")
        st.rerun()
