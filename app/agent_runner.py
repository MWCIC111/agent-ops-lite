"""agent_runner.py — 真实 Agent 运行器（headless，无 streamlit 依赖）

把 8_真实Agent.py 里「调 DeepSeek + 华佗百科 RAG」的核心逻辑抽离到这里，
供两类调用方复用：
  1. 8_真实Agent.py（Streamlit UI 页）—— 用户手动提问，现场演示真实调用。
  2. scripts/seed_real_data.py（headless 播种脚本）—— 批量跑真实问答，
     把真实 Trace 落库，让所有观测页面都有真实数据可看。

所有真实调用都被 agent_ops @trace 采集，自动落 SQLite（agent_ops.db），
其余 8 个观测页面直接消费。

安全：API Key 只从环境变量读取，不写进代码 / 不提交 GitHub。
"""
from __future__ import annotations

import os
import sys
import time
import concurrent.futures  # 4 个垂直 Agent 并行执行，降低多步编排耗时

# ---- 让本模块能 import 到仓库根的 agent_ops 与 app/ 下的 rag_retriever ----
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ops import Collector, record_step, trace, SQLiteStore  # noqa: E402
from agent_ops.cost import MODEL_PRICE  # noqa: E402
from rag_retriever import retrieve, count as rag_count  # noqa: E402

# 最近一次检索命中片段（供 UI 展示），模块级缓存。
_LAST_HITS: list = []

# DeepSeek OpenAI 兼容端点（官方）
OPENAI_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")
store = SQLiteStore(DB_PATH)
collector = Collector(storage=store)


def _timed(fn):
    t0 = time.perf_counter()
    result = fn()
    ms = max(int((time.perf_counter() - t0) * 1000), 1)
    # 如果 fn 返回 tuple，自动展开后追加耗时，避免外层嵌套解包。
    if isinstance(result, tuple):
        return *result, ms
    return result, ms


def _deepseek_chat(model: str, messages: list, temperature: float = 0.3,
                   max_tokens: int = 400):
    from openai import OpenAI

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "环境变量 DEEPSEEK_API_KEY 未设置。请在 .env 中配置并重启服务。"
        )

    client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=model, messages=messages, temperature=temperature,
        max_tokens=max_tokens,
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

    state: dict = {"question": question}

    # 1) 共享 State：知识检索
    ctx, hits, rms = _timed(lambda: _retrieve_context(question, top_k=4))
    _LAST_HITS[:] = hits
    state["context"] = ctx
    record_step("共享State · 知识检索", model=model, tool="BM25检索(华佗百科)",
                tokens_in=0, tokens_out=0, latency_ms=rms)

    # 2) Orchestrator 集中式任务编排
    orch_msgs = [
        {"role": "system", "content": "你是研发管家的 Orchestrator，集中式编排 4 个垂直 Agent"
         "（抗原设计 / 方案规划 / 故障诊断 / 资料整理）。基于问题与检索依据，向每个 Agent 下发结构化子任务指令。"},
        {"role": "user", "content": f"问题：{question}\n检索依据摘要：\n{ctx[:1500]}"},
    ]
    orch, oin, oout, oms = _timed(lambda: _deepseek_chat(model, orch_msgs))
    state["orchestration"] = orch
    record_step("Orchestrator · 任务编排", model=model, tokens_in=oin, tokens_out=oout, latency_ms=oms)

    # 3) 4 个垂直 Agent 并行（共享 State 串联，彼此无依赖，并行调用降低编排耗时）
    def _run_vertical(agent_name: str, sys_prompt: str):
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content":
                f"共享状态：\n- 问题：{question}\n- 检索依据：{ctx[:1000]}\n"
                f"- Orchestrator 指令：{orch[:600]}\n请基于以上输出你的专业结论。"},
        ]
        return agent_name, _timed(lambda m=msgs: _deepseek_chat(model, m, max_tokens=400))

    # 并发执行，按原始顺序收集结果（保持 Trace 步骤顺序稳定）
    _results: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        _futs = {ex.submit(_run_vertical, n, sp): n
                 for n, sp in BUTLER_AGENTS.items()}
        for _f in concurrent.futures.as_completed(_futs):
            _n, _payload = _f.result()
            _results[_n] = _payload

    parts: list = []
    for agent_name, sys_prompt in BUTLER_AGENTS.items():
        ans, tin, tout, ms = _results[agent_name]
        record_step(agent_name, model=model, tool="垂直Agent·DeepSeek",
                    tokens_in=tin, tokens_out=tout, latency_ms=ms)
        state[agent_name] = ans
        parts.append(f"### {agent_name}\n{ans}")

    # 4) 置信度融合 + 三层幻觉抑制
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
    answer, tin, tout, ms = _timed(lambda: _deepseek_chat(model, messages))
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
