"""
8_真实Agent.py — 真实 Agent 调用（接入 DeepSeek API，被 agent_ops @trace 采集）

把 Demo 从「模拟数据」升级为「真实可观测」：
  一次真实 LLM 调用 -> 真实 token / 延迟 / 成本 -> 自动落 SQLite -> 其余 8 个观测页面直接消费。

前置（详见接入指南）：
  1. 环境变量 DEEPSEEK_API_KEY 已配置（systemd 通过 EnvironmentFile 加载）
  2. app/requirements.txt 已加 openai
  3. agent_ops/cost.py 已把未知模型兜底为 0 价（演示阶段避免 KeyError）
  4. app/demo_data.py 的 load_demo_traces() 已追加真实 SQLite trace（见指南 / apply_patches.py）

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

# 兜底：演示阶段把 deepseek 模型按 0 价计，避免价格波动影响展示。
# 若要显示真实成本，请在 agent_ops/cost.py 的 MODEL_PRICE 中加入该模型单价。
for _m in ("deepseek-chat", "deepseek-reasoner", "deepseek-coder"):
    MODEL_PRICE.setdefault(_m, (0.0, 0.0))

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")
store = SQLiteStore(DB_PATH)
collector = Collector(storage=store)

# DeepSeek OpenAI 兼容端点（官方）
OPENAI_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

SCENARIOS = {
    "研发管家 · 研发问答": "检索 -> 生成 -> 校验（多步 + 校验，对应简历核心项目）",
    "知源 · RAG 问答": "检索增强生成（RAG）",
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


def _mock_retrieve(question: str) -> str:
    # 演示用：真实场景这里接 Milvus / Elasticsearch 向量检索。
    return f"（检索依据）与「{question}」相关的内部知识库片段……"


@trace(agent="真实 Agent · DeepSeek", collector=collector)
def run_real_agent(scenario: str, question: str, model: str) -> str:
    """真实 Agent 入口，被 @trace 采集；每一步记录真实 token 与耗时。"""
    MODEL_PRICE.setdefault(model, (0.0, 0.0))  # 极端兜底：用户改了模型名也不崩

    if scenario == "通用问答":
        answer, tin, tout, ms = _timed(
            lambda: _deepseek_chat(model, [{"role": "user", "content": question}])
        )
        record_step("内容生成", model=model, tokens_in=tin, tokens_out=tout, latency_ms=ms)
        return answer

    if scenario == "知源 · RAG 问答":
        ctx, rms = _timed(lambda: _mock_retrieve(question))
        record_step("知识检索", model=model, tool="向量检索(Milvus)",
                    tokens_in=0, tokens_out=0, latency_ms=rms)
        messages = [
            {"role": "system", "content": "你是企业知识库助手，仅基于检索依据作答。"},
            {"role": "user", "content": f"依据：{ctx}\n问题：{question}"},
        ]
        answer, tin, tout, ms = _timed(lambda: _deepseek_chat(model, messages))
        record_step("内容生成", model=model, tokens_in=tin, tokens_out=tout, latency_ms=ms)
        return answer

    # 研发管家 · 研发问答：检索 -> 生成 -> 校验
    ctx, rms = _timed(lambda: _mock_retrieve(question))
    record_step("知识检索", model=model, tool="向量检索(Milvus)",
                tokens_in=0, tokens_out=0, latency_ms=rms)
    gen_msgs = [
        {"role": "system", "content": "你是研发助手，基于检索依据给出工程结论。"},
        {"role": "user", "content": f"依据：{ctx}\n问题：{question}"},
    ]
    answer, tin, tout, ms = _timed(lambda: _deepseek_chat(model, gen_msgs))
    record_step("内容生成", model=model, tokens_in=tin, tokens_out=tout, latency_ms=ms)
    verify_msgs = [
        {"role": "system", "content": "校验：结论必须回指检索依据，否则判失败。"},
        {"role": "user", "content": f"依据：{ctx}\n结论：{answer}\n是否回指依据？(仅答 是/否)"},
    ]
    verdict, vin, vout, vms = _timed(lambda: _deepseek_chat(model, verify_msgs))
    record_step("结果校验", model=model, tokens_in=vin, tokens_out=vout, latency_ms=vms)
    return f"{answer}\n\n（校验：{verdict.strip()}）"


# ------------------- UI -------------------
st.info(f"当前端点：{OPENAI_BASE_URL} ｜ 默认模型：{DEFAULT_MODEL}")
model = st.text_input("模型名", value=DEFAULT_MODEL,
                      help="例如 deepseek-chat、deepseek-reasoner；需在 .env 中已配置 DEEPSEEK_API_KEY")
scenario = st.selectbox("场景（对应简历项目）", list(SCENARIOS.keys()),
                        format_func=lambda k: f"{k} — {SCENARIOS[k]}")
question = st.text_area("提问", value="如何设计多 Agent 的共享状态？", height=80)

if st.button("运行真实 Agent", type="primary"):
    if not question.strip():
        st.warning("请先输入问题")
    else:
        with st.spinner("正在调用 DeepSeek API ..."):
            try:
                answer = run_real_agent(scenario, question.strip(), model.strip())
                st.success("调用完成，已落库 agent_ops.db（其余 8 个页面可直接看到真实 Trace）")
                st.subheader("回答")
                st.write(answer)
            except Exception as e:  # noqa: BLE001
                st.error(f"调用失败：{type(e).__name__}: {e}")
                st.info("检查：/home/ubuntu/agent-ops-lite/.env 是否配置了 DEEPSEEK_API_KEY；systemctl 是否已重启加载环境变量")

# ------------------- 最近真实 Trace -------------------
st.divider()
st.subheader("最近真实 Trace（来自 agent_ops.db）")
traces = collector.traces()
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
