"""seed_real_data.py — 用真实 DeepSeek + 华佗百科 RAG 生成真实 Trace 落库

把 Demo 从「模拟数据」升级为「真实数据驱动」：批量跑真实 Agent 问答，
每次调用都被 agent_ops @trace 采集并落 SQLite（agent_ops.db），
其余 8 个观测页面（链路追踪 / 工具分析 / 成本核算 / 告警 / 版本对比 / 灰度 / 拓扑 / 总览）
即可直接消费真实 token / 延迟 / 成本 / 工具 / 知识库召回。

headless，无 streamlit 依赖。在服务器（或本地）运行：

  # 默认：40 条知源RAG + 8 条研发管家 + 15 条通用 + 3 条真实失败，时间戳散布 14 天
  python3 scripts/seed_real_data.py

  # 自定义体量
  python3 scripts/seed_real_data.py --rag 60 --butler 10 --general 20 --failures 3 --spread-days 14

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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_runner  # noqa: E402

# ---------------------------------------------------------------------------
# 代表性问答语料（真实调用时答案由 DeepSeek 生成；这里只提供问题）
# ---------------------------------------------------------------------------

# 知源 · RAG 问答：华佗百科医疗条目（疾病 / 药品 / 症状）
RAG_QUERIES = [
    "糖尿病应该怎么控制饮食？",
    "高血压患者的日常注意事项有哪些？",
    "感冒了吃什么药比较好？",
    "布洛芬和对乙酰氨基酚有什么区别？",
    "孕妇感冒了能用什么药？",
    "阿莫西林是抗生素吗？有什么副作用？",
    "高血脂患者饮食要注意什么？",
    "胃溃疡吃什么食物有助于恢复？",
    "过敏性鼻炎怎么治疗？",
    "痛风发作时可以热敷吗？",
    "小孩发烧多少度需要去医院？",
    "他汀类药物需要长期吃吗？",
    "慢性胃炎怎么调理？",
    "偏头痛该怎么缓解？",
    "哮喘患者平时需要注意什么？",
    "补钙吃什么食物好？",
    "失眠有什么自然改善的方法？",
    "乙肝携带者需要治疗吗？",
    "甲状腺结节严重吗？",
    "骨质疏松怎么预防？",
    "低血压有什么症状？",
    "腹泻的时候能吃东西吗？",
    "咳嗽一直不好是怎么回事？",
    "维生素C能预防感冒吗？",
    "糖尿病患者能吃水果吗？",
    "高血压能吃鸡蛋吗？",
    "抗生素滥用有什么危害？",
    "退烧药和美林有什么区别？",
    "贫血吃什么补得快？",
    "肩周炎怎么锻炼恢复？",
    "前列腺炎有什么症状？",
    "乳腺增生严重吗？",
    "幽门螺杆菌感染怎么治？",
    "湿疹怎么护理？",
    "腰椎间盘突出要注意什么？",
    "甲减和甲亢有什么区别？",
    "便秘怎么调理饮食？",
    "心律失常严重吗？",
    "带状疱疹会传染吗？",
    "饭后多久运动比较好？",
    "降压药漏服了要补吗？",
    "流感疫苗有必要打吗？",
    "长期久坐有什么危害？",
    "喝酒对肝脏有什么伤害？",
    "缺钙会有什么表现？",
    "慢性咽炎怎么缓解？",
    "尿酸高一定会痛风吗？",
    "睡前喝牛奶有助于睡眠吗？",
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
    print(f"  知识库：华佗百科（{agent_runner.rag_count()} 条）｜ 落库：agent_ops.db", flush=True)
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


if __name__ == "__main__":
    main()
