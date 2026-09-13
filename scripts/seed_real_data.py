"""seed_real_data.py — 用真实 DeepSeek + BM25 知识库 RAG 生成真实 Trace 落库

把 Demo 从「模拟数据」升级为「真实数据驱动」：批量跑真实 Agent 问答，
每次调用都被 agent_ops @trace 采集并落 SQLite（agent_ops.db），
其余观测页面
即可直接消费真实 token / 延迟 / 成本 / 工具 / 知识库召回。

headless，无 streamlit 依赖。在服务器（或本地）运行：

  # 默认：40 条知源RAG + 8 条研发管家 + 15 条通用 + 3 条真实失败，时间戳散布 14 天
  python3 scripts/seed_real_data.py

  # 自定义体量
  python3 scripts/seed_real_data.py --rag 60 --butler 10 --general 20 --failures 3 --spread-days 14

  注意：默认体量约 66 个场景（研发管家再加多步调用），不要挂高频 cron；
  定时播种建议缩小为 --rag 5 --butler 2 --general 3 --failures 1。

  # 后台运行（推荐，研发管家为多步编排较慢）
  nohup python3 scripts/seed_real_data.py > seed.log 2>&1 &

安全：API Key 从环境变量 DEEPSEEK_API_KEY 读取；本脚本不写任何密钥。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta

# Windows 默认 GBK 控制台无法打印 ✔/✗/▶：统一强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LOCK_FILE = os.path.join(_REPO_ROOT, ".seed.lock")


def _load_dotenv() -> None:
    """从项目根 .env 注入环境变量（不依赖 python-dotenv）。"""
    dotenv = os.path.join(_REPO_ROOT, ".env")
    if not os.path.exists(dotenv):
        return
    with open(dotenv, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and os.environ.get(key) is None:
                os.environ[key] = value


_load_dotenv()

import agent_runner  # noqa: E402

# ---------------------------------------------------------------------------
# 代表性问答语料（真实调用时答案由 DeepSeek 生成；这里只提供问题）
# ---------------------------------------------------------------------------

# 知源 · RAG 问答：IVD 器审指导原则条目（注册申报 / 性能评价 / 临床评价等）
RAG_QUERIES = [
    "体外诊断试剂稳定性研究需要提供哪些资料？",
    "申请注册时，说明书里必须写清楚哪些内容？",
    "全自动化学发光免疫分析仪的注册申报资料有哪些要求？",
    "核酸检测试剂的引物探针设计有什么规定？",
    "抗原检测试剂的主要原材料研究要做什么？",
    "试剂的批间差怎么控制，精密度指标怎么定？",
    "交叉反应和干扰试验在什么情况下必须做？",
    "最低检测限怎么确定和验证？",
    "阳性判断值的研究需要多少样本？",
    "临床试验的样本量怎么估算？",
    "企业参考品和质控品有什么区别和要求？",
    "试剂的有效期和包装研究要看哪些方面？",
    "钩状效应是什么意思，需要做验证吗？",
    "肿瘤标志物定量检测试剂的审评重点关注什么？",
    "基因多态性检测试剂要提交哪些临床评价资料？",
    "体外诊断设备的软件研究资料包括哪些？",
    "样本采集和处理的验证要怎么做？",
    "注册单元和型号规格怎么划分？",
    "多重病原体联检试剂的分析性能怎么评价？",
    "试剂说明书里的预期用途应该怎么表述？",
    "线性范围怎么确定？",
    "准确度评价要做哪些试验？",
    "分析特异性研究包括哪些内容？",
    "校准品的溯源要求是什么？",
    "免疫组化试剂的审评要点有哪些？",
    "PCR 试剂的污染控制要求有哪些？",
    "血气分析仪注册需要提交哪些资料？",
    "参考区间怎么建立和验证？",
    "试剂盒的运输稳定性怎么验证？",
    "自测用体外诊断试剂有什么特殊要求？",
    "体外诊断试剂的临床评价有哪几种路径？",
    "同型半胱氨酸检测试剂的注册要求是什么？",
]

# 研发管家 · 研发问答：生物/IVD 研发场景（对应简历「研发管家」）
BUTLER_QUERIES = [
    "如何设计多 Agent 的共享状态？",
    "设计一种新型传染病抗原的检测方案，请给出研发路线。",
    "现有胶体金试纸灵敏度不足，如何定位故障并改进？",
    "请规划一个体外诊断试剂从立项到注册的全流程方案。",
    "抗原表达量低，可能的原因和优化方向有哪些？",
    "如何为研发管家系统设计置信度融合与幻觉抑制机制？",
    "某批次试剂盒批间差过大，如何做故障诊断？",
    "请给出病原体多重联检的实验方案规划。",
    "抗体交叉反应如何排查和解决？",
    "研发资料太多难以沉淀，如何设计结构化整理流程？",
    "如何评估一个 IVD 项目的研发风险？",
    "量产转移阶段工艺不稳定，给故障诊断与对策。",
    "设计一种肿瘤早筛标志物的发现与验证路线。",
    "如何把文献中的方法转化为可落地的实验 SOP？",
    "试剂稳定性研究应该怎么设计方案？",
]

# 通用问答：单步直接调用
GENERAL_QUERIES = [
    "用一句话解释什么是大语言模型。",
    "Python 里 list 和 tuple 的区别是什么？",
    "什么是检索增强生成（RAG）？",
    "解释一下 LangGraph 的核心思想。",
    "什么是向量数据库的相似度检索？",
    "Docker 和虚拟机的区别是什么？",
    "什么是 Agent 的 tool calling？",
    "解释一下机器学习里的过拟合。",
    "什么是 API 的限流与熔断？",
    "如何用一句话向非技术同事解释微服务？",
    "什么是数据库索引，为什么能加速查询？",
    "解释一下什么是 A/B 测试。",
    "什么是灰度发布？为什么不全量一把梭？",
    "什么是 LLM 的幻觉，怎么缓解？",
    "解释一下 Transformer 的注意力机制。",
    "什么是提示词工程（Prompt Engineering）？",
    "如何用 Python 读取一个 CSV 文件？",
    "什么是持续集成和持续部署（CI/CD）？",
    "解释一下什么是可观测性（Observability）。",
    "什么是 JSON Schema，它用来做什么？",
]


def _cycle(pool: list[str], n: int) -> list[str]:
    if n <= len(pool):
        return pool[:n]
    # 超出池子则循环填充，保证数量
    return [pool[i % len(pool)] for i in range(n)]


def backdate(days_ago: float) -> None:
    """把刚产生的最后一条 Trace 的时间戳回拨到指定天数前，便于趋势图展示。"""
    trs = agent_runner.collector.traces()
    if not trs:
        return
    t = trs[-1]
    t.started_at = datetime.now() - timedelta(
        days=days_ago,
        hours=random.uniform(0, 23),
        minutes=random.uniform(0, 59),
    )
    agent_runner.store.save(t)  # INSERT OR REPLACE by trace_id，更新时间戳


def _run(scenario: str, query: str, model: str, spread_days: int, backdate_on: bool) -> bool:
    try:
        agent_runner.run_real_agent(scenario, query, model)
        if backdate_on and spread_days > 0:
            backdate(random.uniform(0, spread_days))
        return True
    except Exception as e:  # noqa: BLE001
        # 真实失败也落库（@trace 捕获异常记 failed），用于演示告警/异常页
        if backdate_on and spread_days > 0:
            try:
                backdate(random.uniform(0, spread_days))
            except Exception:
                pass
        print(f"  ✗ 失败（已落库为 failed）：{type(e).__name__}: {e}", flush=True)
        return False


def _pid_alive(pid: int) -> bool:
    """POSIX 下探测进程是否存活；其他平台保守视为存活。"""
    if pid <= 0:
        return False
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock() -> bool:
    """并发播种防重入：进程存活的旧锁直接拒绝，残留锁则接管。"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                pid = int(f.read().strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if _pid_alive(pid):
            print(f"✗ 已有播种任务在运行（PID {pid}），本次退出。", flush=True)
            return False
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def _run_main(args: argparse.Namespace) -> None:
    if not agent_runner.OPENAI_API_KEY:
        print("✗ 未检测到 DEEPSEEK_API_KEY 环境变量，无法调用真实 API。请先配置后重试。", flush=True)
        sys.exit(1)

    backdate_on = not args.no_backdate
    spread = args.spread_days
    total = args.rag + args.butler + args.general + args.failures
    done = 0
    ok = 0

    print(f"▶ 开始真实数据播种：RAG={args.rag} 研发管家={args.butler} 通用={args.general} "
          f"失败={args.failures} 模型={args.model} 散布={spread}天", flush=True)
    print(f"  知识库：{agent_runner.corpus_label()}（{agent_runner.rag_count()} 条）｜ 落库：agent_ops.db", flush=True)
    t0 = time.time()

    # 1) 知源 RAG
    for i, q in enumerate(_cycle(RAG_QUERIES, args.rag), 1):
        if _run("知源 · RAG 问答", q, args.model, spread, backdate_on):
            ok += 1
        done += 1
        print(f"  [{done}/{total}] 知源RAG #{i}: {q[:24]}...", flush=True)
        time.sleep(0.3)

    # 2) 研发管家（多步编排，较慢）
    for i, q in enumerate(_cycle(BUTLER_QUERIES, args.butler), 1):
        if _run("研发管家 · 研发问答", q, args.model, spread, backdate_on):
            ok += 1
        done += 1
        print(f"  [{done}/{total}] 研发管家 #{i}: {q[:24]}...", flush=True)
        time.sleep(0.5)

    # 3) 通用问答
    for i, q in enumerate(_cycle(GENERAL_QUERIES, args.general), 1):
        if _run("通用问答", q, args.model, spread, backdate_on):
            ok += 1
        done += 1
        print(f"  [{done}/{total}] 通用 #{i}: {q[:24]}...", flush=True)
        time.sleep(0.3)

    # 4) 注入真实失败（用非法模型名触发真实 API 错误，@trace 记为 failed）
    for i in range(args.failures):
        # 用不存在的模型名 -> DeepSeek 返回 404 -> 真实异常 -> failed Trace
        if _run("通用问答", GENERAL_QUERIES[i % len(GENERAL_QUERIES)], "__invalid_model__", spread, backdate_on):
            ok += 1
        done += 1
        print(f"  [{done}/{total}] 注入失败 #{i+1}", flush=True)

    cost = agent_runner.store.count()
    print(f"✔ 播种完成：成功 {ok}/{total}，耗时 {time.time()-t0:.0f}s，"
          f"agent_ops.db 现有 {cost} 条 Trace。", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="用真实 DeepSeek+RAG 生成真实 Trace 落库")
    ap.add_argument("--rag", type=int, default=40, help="知源 RAG 问答条数")
    ap.add_argument("--butler", type=int, default=8, help="研发管家多步编排条数")
    ap.add_argument("--general", type=int, default=15, help="通用问答条数")
    ap.add_argument("--failures", type=int, default=3, help="注入真实失败条数（用非法模型触发真实 API 错误）")
    ap.add_argument("--spread-days", type=int, default=14, help="时间戳散布天数（0=不回拨，全部为现在）")
    ap.add_argument("--model", type=str, default=agent_runner.DEFAULT_MODEL, help="使用的模型名")
    ap.add_argument("--no-backdate", action="store_true", help="不回拨时间戳（全部为当前时间）")
    args = ap.parse_args()

    if not _acquire_lock():
        sys.exit(2)
    try:
        _run_main(args)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
