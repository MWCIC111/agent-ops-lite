"""
8_真实Agent.py — 真实 Agent 调用（接入 DeepSeek API，被 agent_ops @trace 采集）

把 Demo 从「模拟数据」升级为「真实可观测」：
  一次真实 LLM 调用 -> 真实 token / 延迟 / 成本 -> 自动落 SQLite -> 其余观测页面直接消费。

三个场景对应简历项目：
  - 研发管家 · 研发问答：真 LangGraph 集中式多 Agent 编排（Orchestrator + 共享 State +
    4 垂直 Agent 经 Send 扇出 + 置信度融合·三层幻觉抑制 + 低置信转人工回写闭环），每一步 record_step，Trace 出现多节点。
  - 知源 · RAG 问答：BM25 真实检索（语料见 rag_retriever.corpus_name）增强生成。
  - 通用问答：单步直接调用。

真实运行逻辑抽离在 app/agent_runner.py（UI 与 headless 播种脚本共用）。

安全注意：API Key 只从环境变量读取，不要写进代码或提交到 GitHub。
"""
from __future__ import annotations

import os
import sys

import streamlit as st
from common import show_clock, page_visit
from op_log import log_operation

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
from rag_retriever import corpus_label  # noqa: E402
from agent_ops import SQLiteStore  # noqa: E402

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")

SCENARIOS = {
    "研发管家 · 研发问答": "真 LangGraph 集中式多 Agent：Orchestrator + 共享 State + 4 垂直 Agent(Send扇出) + 置信度融合·三层幻觉抑制 + 低置信转人工回写闭环",
    "知源 · RAG 问答": f"检索增强生成（BM25 · {corpus_label()}）",
    "通用问答": "单步直接调用",
}

# 每个场景对应的示例提问，按所选场景动态展示，点击即填入提问框。
# 「知源」用能命中当前语料的口语化问题；「研发管家」用 IVD 研发场景问题，
# 使检索 -> 4 Agent 协作 -> 置信度融合整条链路都被真实演示到。
EXAMPLE_QUESTIONS = {
    "研发管家 · 研发问答": [
        "抗原表达量低，可能的原因和优化方向有哪些？",
        "设计一个测降钙素原的化学发光试剂，研发方案该怎么规划？",
        "某批次试剂盒批间差过大，如何定位故障并改进？",
        "抗原设计阶段如何降低脱靶（交叉反应）风险？",
        "从靶点确认到试剂盒定型，整体研发流程分哪几个阶段？",
    ],
    "知源 · RAG 问答": [
        "体外诊断试剂稳定性研究需要提供哪些资料？",
        "申请注册时，说明书里必须写清楚哪些内容？",
        "最低检测限怎么确定和验证？",
        "试剂的批间差怎么控制，精密度指标怎么定？",
        "临床试验的样本量怎么估算？",
    ],
    "通用问答": [
        "用一句话解释什么是大语言模型",
        "LangGraph 和 LangChain 的区别是什么？",
        "什么是检索增强生成（RAG）？",
        "如何向非技术人员解释什么是 Agent？",
        "Transformer 注意力机制的核心思想是什么？",
    ],
}

st.set_page_config(page_title="真实 Agent · agent-ops-lite", layout="wide")
st.title("真实 Agent 调用（DeepSeek API）")
show_clock()
page_visit("真实Agent")
st.caption("调用 DeepSeek 真实大模型，经 @trace 采集真实 token / 延迟 / 成本，落库后全面板可消费")

# 提问框初始值（示例问题点击后会被覆盖）
if "question_input" not in st.session_state:
    st.session_state.question_input = EXAMPLE_QUESTIONS["研发管家 · 研发问答"][0]


# ------------------- UI -------------------
st.info(f"当前端点：{OPENAI_BASE_URL} ｜ 默认模型：{DEFAULT_MODEL} ｜ 知识库：{corpus_label()}（{rag_count()} 条）")
model = st.text_input("模型名", value=DEFAULT_MODEL,
                      help="例如 deepseek-chat、deepseek-reasoner；需在 .env 中已配置 DEEPSEEK_API_KEY")
scenario = st.selectbox("场景（对应简历项目）", list(SCENARIOS.keys()),
                        format_func=lambda k: f"{k} — {SCENARIOS[k]}")

# 按当前场景展示示例问题，点击按钮即填入提问框
examples = EXAMPLE_QUESTIONS[scenario]
scenario_idx = list(SCENARIOS.keys()).index(scenario)
st.caption("💡 快速选择示例问题（点击即填入）：")
cols = st.columns(len(examples))
for i, ex in enumerate(examples):
    if cols[i].button(ex, key=f"qex_s{scenario_idx}_q{i}"):
        st.session_state.question_input = ex
        st.rerun()

# 提示当前已选示例问题，避免用户以为没填入
if st.session_state.question_input in examples:
    st.caption(f"✅ 已填入示例问题：{st.session_state.question_input}")

question = st.text_area("提问", key="question_input", height=80)

if st.button("运行真实 Agent", type="primary"):
    if not question.strip():
        st.warning("请先输入问题")
    else:
        spinner_text = "正在调用 DeepSeek API（研发管家为多步编排，约 6 次 LLM 调用）..." \
            if scenario.startswith("研发管家") else "正在调用 DeepSeek API ..."
        with st.spinner(spinner_text):
            try:
                answer = run_real_agent(scenario, question.strip(), model.strip())
                st.success("调用完成，已落库 agent_ops.db（其余观测页面可直接看到真实 Trace）")
                log_operation("真实Agent", "运行成功", f"{scenario}：{question.strip()[:60]}")
                st.subheader("回答")
                st.markdown(answer)
                if _LAST_HITS:
                    st.subheader(f"检索依据（BM25 · {corpus_label()}）")
                    for h in _LAST_HITS:
                        with st.expander(f"▸ {h['title']}（score={h['score']:.2f}）"):
                            st.caption(h["source"])
                            st.write(h["content"])
            except Exception as e:  # noqa: BLE001
                st.error(f"调用失败：{type(e).__name__}: {e}")
                log_operation("真实Agent", "运行失败", f"{scenario}：{question.strip()[:60]} - {type(e).__name__}")
                st.info("检查：项目根目录 .env 是否配置了 DEEPSEEK_API_KEY；重启服务以重新加载环境变量")

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
