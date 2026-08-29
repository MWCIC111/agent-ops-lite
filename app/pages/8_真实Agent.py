"""
8_真实Agent.py — 真实 Agent 调用（接入 DeepSeek API，被 agent_ops @trace 采集）

把 Demo 从「模拟数据」升级为「真实可观测」：
  一次真实 LLM 调用 -> 真实 token / 延迟 / 成本 -> 自动落 SQLite -> 其余 8 个观测页面直接消费。

三个场景对应简历项目：
  - 研发管家 · 研发问答：LangGraph 式集中式多 Agent 编排（Orchestrator + 共享 State +
    4 垂直 Agent + 置信度融合·三层幻觉抑制），每一步 record_step，Trace 出现多节点。
  - 知源 · RAG 问答：BM25 真实检索（华佗百科知识库）增强生成。
  - 通用问答：单步直接调用。

前置（详见接入指南）：
  1. 环境变量 DEEPSEEK_API_KEY 已配置（systemd 通过 EnvironmentFile 加载）
  2. app/requirements.txt 已加 openai / jieba / rank_bm25
  3. 未知模型兜底已在 agent_ops 核心库内置（避免 KeyError）
  4. app/demo_data.py 的 load_demo_traces() 已合并真实 SQLite trace（随仓库提交，无需补丁）

安全注意：API Key 只从环境变量读取，不要写进代码或提交到 GitHub。
"""
from __future__ import annotations

import os
import sys
import time

import streamlit as st

# ---- 让 app/pages/ 能 import 到仓库根的 agent_ops ----
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_ops import Collector, record_step, trace, SQLiteStore  # noqa: E402
from agent_ops.cost import MODEL_PRICE  # noqa: E402
from rag_retriever import retrieve, count as rag_count  # noqa: E402

# 兜底：演示阶段把 deepseek 模型按 0 价计，避免价格波动影响展示。
# 若要显示真实成本，请在 agent_ops/cost.py 的 MODEL_PRICE 中加入该模型单价。
for _m in ("deepseek-chat", "deepseek-reasoner", "deepseek-coder"):
    MODEL_PRICE.setdefault(_m, (0.0, 0.0))

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")
store = SQLiteStore(DB_PATH)
collector = Collector(storage=store)

# 最近一次检索命中的片段（供 UI 展示），模块级缓存跨 rerun 保留。
_LAST_HITS: list = []

# DeepSeek OpenAI 兼容端点（官方）
OPENAI_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

SCENARIOS = {
    "研发管家 · 研发问答": "LangGraph 式集中式多 Agent：Orchestrator + 共享 State + 4 垂直 Agent + 置信度融合·三层幻觉抑制",
    "知源 · RAG 问答": "检索增强生成（BM25 · 华佗百科知识库）",
    "通用问答": "单步直接调用",
}

st.set_page_config(page_title="真实 Agent · agent-ops-lite", layout="wide")
st.title("真实 Agent 调用（DeepSeek API）")
st.caption("调用 DeepSeek 真实大模型，经 @trace 采集真实 token / 延迟 / 成本，落库后全面板可消费")


def _timed(fn):
    t0 = time.perf_counter()
    result = fn()
    ms = max(int((time.perf_counter() - t0) * 1000), 1)
    # 如果 fn 返回 tuple，自动展开后追加耗时，避免外层嵌套解包。
    if isinstance(result, tuple):
        return *result, ms
    return result, ms


def _deepseek_chat(model: str, messages: list, temperature: float = 0.3):
    from openai import OpenAI

    if not OPENAI_API_KEY:
        raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置。请在 /home/ubuntu/agent-ops-lite/.env 中配置并重启服务。")

    client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=model, messages=messages, temperature=temperature
    )
    usage = resp.usage
    return (
        resp.choices[0].message.content,
        int(usage.prompt_tokens or 0),
        int(usage.completion_tokens or 0),
    )


def _retrieve_context(question: str, top_k: int = 3) -> tuple[str, list]:
    """真实 BM25 检索（华佗百科知识库），返回拼接 context 与命中片段列表。"""
    hits = retrieve(question, top_k=top_k)
    ctx = "\n\n".join(f"【{h['title']}】{h['content']}" for h in hits)
    return ctx, hits


# ---------------------------------------------------------------------------
# 研发管家：LangGraph 式集中式多 Agent 编排
#   Orchestrator 统一调度 4 个垂直 Agent，4 个 Agent 通过「共享 State」串联；
#   末段做置信度融合 + 三层幻觉抑制（工具层 / LLM 层 / 输出层）。
#   每一步都是一次真实 LLM 调用，并被 record_step 采集，Trace 出现多节点。
# ---------------------------------------------------------------------------
BUTLER_AGENTS = {
    "抗原设计 Agent": "你是研发管家系统中的「抗原设计」垂直 Agent。基于检索依据与共享状态，给出抗原/表位设计方案（序列倾向、免疫原性权衡、设计风险点）。只输出你的专业结论。",
    "方案规划 Agent": "你是「方案规划」垂直 Agent。把设计任务拆为可执行的研发/实验方案，输出阶段、依赖、排期与验收口径。只输出你的专业结论。",
    "故障诊断 Agent": "你是「故障诊断」垂直 Agent。定位研发链路中的异常、瓶颈与风险，给出根因假设与对策。只输出你的专业结论。",
    "资料整理 Agent": "你是「资料整理」垂直 Agent。汇总前述各 Agent 的结论，整理为结构化交付报告（背景 / 方案 / 风险 / 下一步）。只输出你的专业结论。",
}


@trace(agent="研发管家 · 多Agent编排", collector=collector)
def run_research_butler(question: str, model: str) -> str:
    """真实多步编排：共享State检索 -> Orchestrator编排 -> 4垂直Agent -> 置信度融合。"""
    MODEL_PRICE.setdefault(model, (0.0, 0.0))

    # 集中式共享 State（对应生产 TypedDict，各 Agent 读写同一份上下文）
    state: dict = {"question": question}

    # 1) 共享 State：知识检索（写入 State，供后续 Agent 读取）
    ctx, hits, rms = _timed(lambda: _retrieve_context(question, top_k=4))
    _LAST_HITS[:] = hits
    state["context"] = ctx
    record_step("共享State · 知识检索", model=model, tool="BM25检索(华佗百科)",
                tokens_in=0, tokens_out=0, latency_ms=rms)

    # 2) Orchestrator 集中式任务编排（向 4 个垂直 Agent 下发子任务）
    orch_msgs = [
        {"role": "system", "content": "你是研发管家的 Orchestrator，集中式编排 4 个垂直 Agent"
         "（抗原设计 / 方案规划 / 故障诊断 / 资料整理）。基于问题与检索依据，向每个 Agent 下发结构化子任务指令。"},
        {"role": "user", "content": f"问题：{question}\n检索依据摘要：\n{ctx[:1500]}"},
    ]
    orch, oin, oout, oms = _timed(lambda: _deepseek_chat(model, orch_msgs))
    state["orchestration"] = orch
    record_step("Orchestrator · 任务编排", model=model, tokens_in=oin, tokens_out=oout, latency_ms=oms)

    # 3) 4 个垂直 Agent 串流（共享 State 串联：检索依据 + Orchestrator 指令一起喂入）
    parts: list = []
    for agent_name, sys_prompt in BUTLER_AGENTS.items():
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content":
                f"共享状态：\n- 问题：{question}\n- 检索依据：{ctx[:1000]}\n"
                f"- Orchestrator 指令：{orch[:600]}\n请基于以上输出你的专业结论。"},
        ]
        # lambda 用默认参数固定闭包变量，避免循环末值问题
        ans, tin, tout, ms = _timed(lambda m=msgs: _deepseek_chat(model, m))
        record_step(agent_name, model=model, tool="垂直Agent·DeepSeek",
                    tokens_in=tin, tokens_out=tout, latency_ms=ms)
        state[agent_name] = ans
        parts.append(f"### {agent_name}\n{ans}")

    # 4) 置信度融合 + 三层幻觉抑制（工具层 / LLM 层 / 输出层）
    fuse_msgs = [
        {"role": "system", "content": "你是置信度融合与幻觉抑制模块：综合各垂直 Agent 结论，做三层校验"
         "（工具层事实一致性 → LLM 层逻辑自洽 → 输出层与检索依据对齐），给出最终可信结论与置信度（高/中/低）。"},
        {"role": "user", "content": f"问题：{question}\n检索依据：{ctx[:1000]}\n各 Agent 结论：\n" + "\n\n".join(parts)},
    ]
    fuse, fin, fout, fms = _timed(lambda: _deepseek_chat(model, fuse_msgs))
    record_step("置信度融合 · 三层幻觉抑制", model=model, tokens_in=fin, tokens_out=fout, latency_ms=fms)

    return (
        f"## 研发管家 · 多Agent编排结果\n\n"
        f"{fuse}\n\n---\n\n" + "\n\n".join(parts)
    )


@trace(agent="知源 · RAG问答", collector=collector)
def run_zhiyuan(question: str, model: str) -> str:
    """知源：BM25 真实检索 -> 生成 -> （演示用不含校验，保持轻量）。"""
    MODEL_PRICE.setdefault(model, (0.0, 0.0))
    ctx, hits, rms = _timed(lambda: _retrieve_context(question, 3))
    _LAST_HITS[:] = hits
    record_step("知识检索", model=model, tool="BM25检索(华佗百科)",
                tokens_in=0, tokens_out=0, latency_ms=rms)
    messages = [
        {"role": "system", "content": "你是企业知识库助手，仅基于检索依据作答。"},
        {"role": "user", "content": f"依据：{ctx}\n问题：{question}"},
    ]
    (answer, tin, tout), ms = _timed(lambda: _deepseek_chat(model, messages))
    record_step("内容生成", model=model, tokens_in=tin, tokens_out=tout, latency_ms=ms)
    return answer


@trace(agent="通用问答 · DeepSeek", collector=collector)
def run_general(question: str, model: str) -> str:
    """通用问答：单步直接调用。"""
    MODEL_PRICE.setdefault(model, (0.0, 0.0))
    answer, tin, tout, ms = _timed(
        lambda: _deepseek_chat(model, [{"role": "user", "content": question}])
    )
    record_step("内容生成", model=model, tokens_in=tin, tokens_out=tout, latency_ms=ms)
    return answer


def run_real_agent(scenario: str, question: str, model: str) -> str:
    """场景分发：各子函数自带 @trace，产生对应 agent 名的 Trace。"""
    if scenario == "通用问答":
        return run_general(question, model)
    if scenario == "知源 · RAG 问答":
        return run_zhiyuan(question, model)
    return run_research_butler(question, model)


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
# 统一数据源：直接读 SQLite 落库（与全面板 load_demo_traces 同源），
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
