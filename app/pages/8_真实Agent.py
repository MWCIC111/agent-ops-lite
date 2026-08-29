"""
8_真实Agent.py — 真实 Agent 调用（接入 DeepSeek API，被 agent_ops @trace 采集）

把 Demo 从「模拟数据」升级为「真实可观测」：
  一次真实 LLM 调用 -> 真实 token / 延迟 / 成本 -> 自动落 SQLite -> 其余 8 个观测页面直接消费。

三个场景对应简历项目：
  - 研发管家 · 研发问答：LangGraph 式集中式多 Agent 编排（Orchestrator + 共享 State +
    4 垂直 Agent + 置信度融合·三层幻觉抑制），每一步 record_step，Trace 出现多节点。
  - 知源 · RAG 问答：BM25 真实检索（华佗百科知识库）增强生成。
  - 通用问答：单步直接调用。

真实运行逻辑抽离在 app/agent_runner.py（UI 与 headless 播种脚本共用）。

安全注意：API Key 只从环境变量读取，不要写进代码或提交到 GitHub。
"""
from __future__ import annotations

import os
import sys

import streamlit as st

# ---- 让 app/pages/ 能 import 到仓库根的 agent_ops 与 app/ 下的 agent_runner ----
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_runner import (  # noqa: E402
    DEFAULT_MODEL,
    OPENAI_BASE_URL,
    _LAST_HITS,
    rag_count,
    run_real_agent,
)
from agent_ops import SQLiteStore  # noqa: E402

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")

SCENARIOS = {
    "研发管家 · 研发问答": "LangGraph 式集中式多 Agent：Orchestrator + 共享 State + 4 垂直 Agent + 置信度融合·三层幻觉抑制",
    "知源 · RAG 问答": "检索增强生成（BM25 · 华佗百科知识库）",
    "通用问答": "单步直接调用",
}

st.set_page_config(page_title="真实 Agent · agent-ops-lite", layout="wide")
st.title("真实 Agent 调用（DeepSeek API）")
st.caption("调用 DeepSeek 真实大模型，经 @trace 采集真实 token / 延迟 / 成本，落库后全面板可消费")


# ------------------- UI -------------------
st.info(f"当前端点：{OPENAI_BASE_URL} ｜ 默认模型：{DEFAULT_MODEL} ｜ 知识库：华佗百科（{rag_count()} 条）")
model = st.text_input("模型名", value=DEFAULT_MODEL,
                      help="例如 deepseek-chat、deepseek-reasoner；需在 .env 中已配置 DEEPSEEK_API_KEY")
scenario = st.selectbox("场景（对应简历项目）", list(SCENARIOS.keys()),
                        format_func=lambda k: f"{k} — {SCENARIOS[k]}")
question = st.text_area("提问", value="如何设计多 Agent 的共享状态？", height=80)

if st.button("运行真实 Agent", type="primary"):
    if not question.strip():
        st.warning("请先输入问题")
    else:
        spinner_text = "正在调用 DeepSeek API（研发管家为多步编排，约 6 次 LLM 调用）..." \
            if scenario.startswith("研发管家") else "正在调用 DeepSeek API ..."
        with st.spinner(spinner_text):
            try:
                answer = run_real_agent(scenario, question.strip(), model.strip())
                st.success("调用完成，已落库 agent_ops.db（其余 8 个页面可直接看到真实 Trace）")
                st.subheader("回答")
                st.markdown(answer)
                if _LAST_HITS:
                    st.subheader("检索依据（BM25 · 华佗百科）")
                    for h in _LAST_HITS:
                        with st.expander(f"▸ {h['title']}（score={h['score']:.2f}）"):
                            st.caption(h["source"])
                            st.write(h["content"])
            except Exception as e:  # noqa: BLE001
                st.error(f"调用失败：{type(e).__name__}: {e}")
                st.info("检查：/home/ubuntu/agent-ops-lite/.env 是否配置了 DEEPSEEK_API_KEY；systemctl 是否已重启加载环境变量")

# ------------------- 最近真实 Trace -------------------
st.divider()
st.subheader("最近真实 Trace（来自 agent_ops.db）")
# 统一数据源：直接读 SQLite 落库（与全面板 load_traces 同源），
# 重启后历史真实 Trace 仍在，不依赖进程内存。
traces = SQLiteStore(DB_PATH).load()
if not traces:
    st.caption("暂无真实调用记录，运行上方按钮后将出现在此处。")
else:
    t = traces[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Agent", t.agent)
    c2.metric("状态", "成功" if t.status == "success" else "失败")
    c3.metric("Token", f"{t.tokens:,}")
    c4.metric("成本", f"¥{t.cost_usd * 7.2:.4f}")
    rows = [
        {
            "步骤": s.name, "模型": s.model, "工具": s.tool or "-",
            "输入Token": s.tokens_in, "输出Token": s.tokens_out,
            "耗时(ms)": s.latency_ms, "状态": "成功" if s.status == "success" else "失败",
        }
        for s in t.steps
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    st.caption(f"共 {len(traces)} 条真实 Trace 已落库")
