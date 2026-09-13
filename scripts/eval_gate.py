"""eval_gate.py — 置信度门控校准与可分性评测（AUC + 噪声底 + 阈值选点 + 规则对照）

问题背景（本脚本存在的理由）
----------------------------
butler_fusion 原用 sigmoid(top_bm25 / 3.0) 计算「相似度分量」。但 rank_bm25 的
BM25Okapi 原始分实测在 10~120 量级，分母 3.0 让分量恒饱和在 ~1.0 —— 库内（可答）
与库外（不可答）问题拿到几乎相同的分量，AUC≈0.52；配套的「top_bm25 <= 0 则强制低置信」
又因分数恒为正而永不触发（死代码）。结论：三层幻觉抑制里，**检索层的数值门控没生效**。

⚠ 最重要的教训（两次踩坑后固化本条）：**门控信号的最优选择随语料翻转，不可迁移。**
  实测（easy 档，FPR≤10% 最优选点）：

  | 语料 | gold 集大小 | ratio = top1/mean(top4) | top1 绝对分 |
  | --- | --- | --- | --- |
  | 通用域（华佗，QA 块） | 1 块 | **AUC 0.983 / 保留 95.5%** | AUC 0.923 / 保留 76.0% |
  | IVD·section-aware | 约 54 块 | **AUC 0.168 / 保留 0.0%** | AUC 1.000 / 保留 100% |
  | IVD·朴素切分 | 约 36 块 | AUC 0.423 / 保留 0.0% | AUC 1.000 / 保留 100% |

  根因：`ratio` 衡量「top1 是否显著高于第 2~4 名」。**当一个答案由多块共同承载
  （gold 集大），top4 会全部命中同一文档、分数接近 → ratio 恒等于 1.0，信号自毁。**
  所以任何写死在某个语料上的门控常数都会失效——本脚本因此同时输出
  [D] 噪声底与间隔、[E] 复合规则对照，供换语料后重新标定。

本脚本做五件事
--------------
  [A] 在**线上真正可得的信号**上算 AUC —— 线上 retrieve(top_k=4) 只返回 4 个分数，
      归一化只能用这 4 个的内部统计量（与「离线全库口径」的关键区别）；
  [B] 给出离线全库口径作参考，并显式标注它线上复现不了；
  [C] 在 FPR 约束下扫阈值，输出 TPR/FPR 与混淆计数，作为门控常数的校准依据；
  [D] **噪声底与间隔**：随机 token 探针的 top1 分布（「完全无意义查询」的得分水平）
      + 正例 min / 负例 max。目的是暴露**「AUC 很高但间隔贴在噪声底」的假完美**：
      实测 IVD 语料上 AUC=1.000，但正例 min 30.32 vs 负例 max 29.28，间隔仅 1.04 分，
      而噪声底 p90 = 25.9 —— 这种阈值换个问法就会翻，不能对外宣称成可靠的拦截能力。
  [E] 复合规则对照（abs / ratio / gap 单选点，AND / OR 组合），用于在单信号失效时
      选出更保守的规则。

正例（库内） = 评测集 query；负例（库外） = 内置 NEG_QUERIES。
⚠ 换语料必须重跑本脚本；且 easy 档正例取自文档标题，与真实口语提问存在**风格差异**，
  阈值迁移性未验证（要可信数字必须跑 hard 档，见 build_eval_set.py --llm）。

用法：
  python scripts/eval_gate.py                                    # 线上当前语料
  python scripts/eval_gate.py --corpus ivd_naive --show-leaks    # 对照朴素切分语料
  python scripts/eval_gate.py --sweep 3,5 --top-k 4 --max-fpr 0.05
  python scripts/eval_gate.py --markdown gate_report.md
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

import numpy as np
from jieba import lcut
from rank_bm25 import BM25Okapi

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_OUT_DIR = os.path.join(_REPO_ROOT, "rag_data")

_EVAL_SPEC = {
    "ivd": "eval_set_ivd.jsonl",
    "ivd_naive": "eval_set_ivd_naive.jsonl",
    "general": "eval_set.jsonl",
}

# 库外问题集（2026-09-13 针对 IVD 语料重设）
#
# ⚠ 换语料必须重设本列表。上一版（通用域）包含「试剂稳定性研究应该怎么设计方案？」
# 「请规划一个体外诊断试剂从立项到注册的全流程方案」等 —— 这些在 IVD 语料里**已被
# NMPA 指导原则正文覆盖**，继续当负例属于**标签错误**，会把 FPR 算虚高、把阈值选偏。
# 现按「语料明确不覆盖」的 4 类构造：
#   A 通用闲聊/技术   B 企业运营/商务   C 具体产品故障排查   D 临床诊疗
NEG_QUERIES = [
    # A 通用闲聊 / 通用技术
    "今天武汉天气怎么样？",
    "帮我写一首关于秋天的诗。",
    "Python 的 GIL 是什么？",
    "如何用 Docker 部署 FastAPI 服务？",
    "给我讲讲 LangGraph 的 Send 机制。",
    "推荐几部适合周末看的电影。",
    # B 企业运营 / 商务
    "公司今年第三季度的营业收入是多少？",
    "竞品的试剂盒定价策略是怎样的？",
    "明年的研发预算应该怎么分？",
    "如何给研发团队做季度绩效考核？",
    "这个岗位的薪资区间是多少？",
    # C 具体产品 / 实操故障
    "某批次试剂盒批间差过大，如何定位故障并改进？",
    "抗原表达量低，可能的原因和优化方向有哪些？",
    "现有胶体金试纸灵敏度不足，怎么改进？",
    "上机测试时仪器报错 E203，该怎么处理？",
    "这批校准品的赋值偏低，排查思路是什么？",
    # D 临床诊疗
    "糖尿病患者应该怎么控制饮食？",
    "高血压患者首选哪类降压药？",
    "感冒了吃什么药好得快？",
    "这个病人是否需要手术？请给诊疗建议。",
    "儿童发热退了又烧，是什么原因？",
]

# 人工撰写的**口语化库内问题**（零成本 hard 档代理，2026-09-13 加入）
#
# 为什么必须有这一组：easy 档正例取自「文档标题逐字」，与真实提问存在**风格差异**。
# 实测（IVD 语料，title×3）：easy 档正例 top1 min = 30.32，看着与库外（max 29.28）完美可分；
# 但换成下列人工口语问题（每条 top1 命中的都是**对题的文档**，说明检索本身没问题），
# top1 落在 15.4 ~ 37.6，与库外（0 ~ 29.3）**完全重叠** —— 也就是说：
#   用 easy 档标定出的「AUC 1.000、库内保留 100%」是**查询风格的产物**，
#   按该阈值上线会误拦 13/20 = 65% 的真实库内提问。
# 结论：BM25 分数（无论绝对分还是 ratio）在真实口语提问下**不具备库内/库外判别能力**；
# 检索层门控应降级为「极端无支撑熔断」，把库外拦截交给 LLM 自评 + 输出层对齐那两层。
NATURAL_QUERIES = [
    "体外诊断试剂稳定性研究要提交哪些材料？",
    "申请注册的时候，说明书里必须写清楚哪些内容？",
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
]


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _import_retriever():
    sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))
    import rag_retriever  # noqa: E402

    return rag_retriever


def auc(pos, neg) -> float:
    """AUC（秩和法，含并列平均秩）。0.5=无区分力，1.0=完美区分。"""
    pos, neg = np.asarray(pos, dtype=float), np.asarray(neg, dtype=float)
    allv = np.concatenate([pos, neg])
    order = np.argsort(allv, kind="mergesort")
    ranks = np.empty(len(allv), dtype=float)
    ranks[order] = np.arange(1, len(allv) + 1)
    rp = ranks[: len(pos)].sum()
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def online_features(scores: np.ndarray, k: int) -> dict:
    """[A] 只取 top_k 个分数 —— 线上 retrieve(top_k) 能拿到的全部信息。"""
    top = np.sort(np.asarray(scores, dtype=float))[::-1][:k]
    top1 = float(top[0])
    mean = float(top.mean())
    std = float(top.std())
    gap = float(top[0] - top[1]) if len(top) > 1 else 0.0
    return {
        "ratio = top1/mean(top_k)": top1 / (mean + 1e-9),
        "gap = top1-top2": gap,
        "top1 绝对分": top1,
        "z = (top1-mean)/std": (top1 - mean) / (std + 1e-9),
    }


def offline_features(scores: np.ndarray) -> dict:
    """[B] 全库口径 —— 仅离线可得，线上复现不了，作为参考对照。"""
    s = np.asarray(scores, dtype=float)
    top1 = float(s.max())
    mean, std = float(s.mean()), float(s.std())
    return {"z (全库)": (top1 - mean) / (std + 1e-9)}


def noise_floor(idx, docs, n: int = 200, seed: int = 42):
    """[D] 随机 token 探针的 top1 分布 —— 「完全无意义查询」的得分水平。

    阈值若贴在噪声底附近，说明它只是在区分「查询长度/风格」而不是「库内/库外」，
    换个问法就会失效。返回 (p50, p90)。
    """
    vocab = sorted({w for d in docs for w in set(lcut(d["content"])) if len(w) >= 2})
    if len(vocab) < 4:
        return 0.0, 0.0
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        q = " ".join(rng.sample(vocab, 4))
        vals.append(float(np.max(idx.get_scores(lcut(q)))))
    vals.sort()
    return vals[len(vals) // 2], vals[int(len(vals) * 0.9)]


def pick_threshold(pos, neg, max_fpr: float = 0.10):
    """在 FPR ≤ max_fpr 约束下，选 TPR 最大的阈值。"""
    p_ = np.asarray(pos, dtype=float)
    n_ = np.asarray(neg, dtype=float)
    best = None
    for t in np.unique(np.concatenate([p_, n_])):
        tpr = float((p_ >= t).mean())
        fpr = float((n_ >= t).mean())
        if fpr <= max_fpr and (best is None or tpr > best[1]):
            best = (float(t), tpr, fpr)
    if best is None:
        return None
    t = best[0]
    return {
        "t": t, "tpr": best[1], "fpr": best[2],
        "kept": int((p_ >= t).sum()), "missed": int((p_ < t).sum()),
        "blocked": int((n_ < t).sum()), "leaked": int((n_ >= t).sum()),
    }


def show_leaks(idx, docs, t: float, k: int) -> None:
    """漏过的库外问题 + top1 命中文档，人工复核负例是否真的「库外」。"""
    print(f"  [D-2] 漏过的库外问题（ratio ≥ {t:.3f}，需人工复核是否真属库外）")
    n_leak = 0
    for q in NEG_QUERIES:
        scores = np.asarray(idx.get_scores(lcut(q)), dtype=float)
        if online_features(scores, k)["ratio = top1/mean(top_k)"] >= t:
            n_leak += 1
            i = int(np.argmax(scores))
            print(f"    · {q}")
            print(f"      → top1 = {docs[i].get('title', '')[:46]}（score={scores[i]:.1f}）")
    if not n_leak:
        print("    （无）")


def report_natural(idx, docs, k: int, abs_gate: float, ratio_gate: float) -> dict:
    """[F] 人工口语化**库内**问题的误拦率 + ABS_GATE 灵敏度扫描。

    这是本脚本最重要的一节：它用真实口语提问（而非文档标题）检验门控，
    专门用来拆穿 easy 档造出的「假完美 AUC」。返回各阈值下的 (库内保留, 库外拦截)。
    """
    pairs = []
    for q in NATURAL_QUERIES:
        s = np.asarray(idx.get_scores(lcut(q)), dtype=float)
        f = online_features(s, k)
        pairs.append((f["top1 绝对分"], f["ratio = top1/mean(top_k)"],
                      int(np.argmax(s)), q))
    neg = []
    for q in NEG_QUERIES:
        f = online_features(np.asarray(idx.get_scores(lcut(q)), dtype=float), k)
        neg.append((f["top1 绝对分"], f["ratio = top1/mean(top_k)"]))

    print(f"  [F] 人工口语化**库内**问题（{len(pairs)} 条 · 零成本 hard 档代理）")
    blocked = 0
    for t1, rt, i, _q in pairs:
        blk = rt < ratio_gate and t1 < abs_gate
        blocked += blk
        print(f"    {t1:>6.1f}{rt:>7.2f}  {'✗ 误拦' if blk else '✓ 保留'}  "
              f"{docs[i].get('title', '')[:38]}")
    print(f"    → 当前阈值（abs<{abs_gate} 且 ratio<{ratio_gate}）误拦 "
          f"{blocked}/{len(pairs)} = {blocked/len(pairs)*100:.0f}%"
          f"  ｜ 库内 top1 区间 {min(p[0] for p in pairs):.1f}~{max(p[0] for p in pairs):.1f}")

    print("    ABS_GATE 灵敏度（AND 规则下；库外拦截基数 "
          f"{len(neg)} 条）：")
    print(f"      {'ABS_GATE':>9}{'库内保留':>10}{'库外拦截':>10}")
    out = {}
    for t in (25.4, 20.0, 15.0, 12.0, 0.0):
        kept = sum(1 for t1, rt, _, _ in pairs if not (rt < ratio_gate and t1 < t))
        blk = sum(1 for t1, rt in neg if rt < ratio_gate and t1 < t)
        out[t] = (kept, blk)
        print(f"      {t:>9.1f}{kept/len(pairs)*100:>9.0f}%{blk/len(neg)*100:>9.0f}%")
    return out


def report(scores_pos, scores_neg, max_fpr: float = 0.10):
    """返回 {signal: (auc, 正例med, 正例min, 负例med, 负例max, best)}。"""
    out = {}
    keys = list(online_features(np.zeros(8), 4))
    for key in keys:
        p = [r[key] for r in scores_pos]
        n = [r[key] for r in scores_neg]
        out[key] = (auc(p, n), float(np.median(p)), float(np.min(p)),
                    float(np.median(n)), float(np.max(n)),
                    pick_threshold(p, n, max_fpr))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="置信度门控校准与可分性评测")
    ap.add_argument("--corpus", default=None, help="语料名；默认=线上当前语料")
    ap.add_argument("--docs", default=None, help="语料 jsonl 路径（覆盖 --corpus）")
    ap.add_argument("--eval-set", default=None, help="正例来源（默认随语料推导）")
    ap.add_argument("--sweep", default="3", help="title 重复次数；多个用逗号分隔")
    ap.add_argument("--top-k", type=int, default=4, help="线上 retrieve 的 top_k（默认 4）")
    ap.add_argument("--max-fpr", type=float, default=0.10, help="阈值选点的 FPR 上限")
    ap.add_argument("--show-leaks", action="store_true", help="打印漏过的库外问题及 top1 命中")
    ap.add_argument("--natural", action="store_true",
                    help="打印人工口语化库内问题的误拦率 + ABS_GATE 灵敏度（推荐必开）")
    ap.add_argument("--markdown", default="", help="把结果表写到该 markdown 文件")
    args = ap.parse_args()

    if args.corpus:
        os.environ["RAG_CORPUS"] = args.corpus
    rr = _import_retriever()
    corpus = args.corpus or rr.corpus_name()
    docs_path = args.docs or rr.corpus_path()
    eval_path = args.eval_set or os.path.join(
        _OUT_DIR, _EVAL_SPEC.get(corpus, "eval_set_ivd.jsonl"))

    for p, hint in ((docs_path, "语料"), (eval_path, "评测集")):
        if not os.path.exists(p):
            print(f"✗ {hint}不存在：{p}")
            sys.exit(1)

    online_w = int(getattr(rr, "TITLE_WEIGHT", 0))
    docs = load_jsonl(docs_path)
    samples = load_jsonl(eval_path)
    weights = [int(x) for x in args.sweep.split(",") if x.strip()]
    k = args.top_k

    content_toks = [lcut(d["content"]) for d in docs]
    title_toks = [lcut(d.get("title", "")) for d in docs]

    print(f"语料 {corpus}：{len(docs)} chunks（{os.path.basename(docs_path)}）")
    print(f"正例（库内）{len(samples)} ｜ 负例（库外）{len(NEG_QUERIES)} ｜ "
          f"线上 TITLE_WEIGHT={online_w} ｜ top_k={k}\n")

    md: list[str] = []
    guard = f"FPR≤{args.max_fpr:.2f}"

    for w in weights:
        corpus_toks = [content_toks[i] + title_toks[i] * w for i in range(len(docs))]
        idx = BM25Okapi(corpus_toks)

        np50, np90 = noise_floor(idx, docs)
        name = "content-only" if w == 0 else f"title×{w} + content"
        tag = f"{name}（线上现行）" if w == online_w else name
        print(f"=== {corpus} ｜ {tag} ===")
        print(f"  [D-0] 噪声底（随机 4 词探针 top1）：p50={np50:.1f}  p90={np90:.1f}"
              f"   ← 阈值若贴在此附近，说明只在区分「查询风格」而非「库内/库外」")

        rows = {
            "pos": [online_features(idx.get_scores(lcut(s["query"])), k) for s in samples],
            "neg": [online_features(idx.get_scores(lcut(q)), k) for q in NEG_QUERIES],
        }
        off = {
            "pos": [offline_features(idx.get_scores(lcut(s["query"]))) for s in samples],
            "neg": [offline_features(idx.get_scores(lcut(q))) for q in NEG_QUERIES],
        }

        rep = report(rows["pos"], rows["neg"], args.max_fpr)

        print(f"  [A] 线上可得信号（仅 top_k={k} 内部统计量，线上可复现）")
        print(f"    {'信号':<26}{'AUC':>7}{'正例med':>9}{'正例min':>9}"
              f"{'负例med':>9}{'负例max':>9}{'间隔':>8}   区分力")
        print("    " + "-" * 84)
        for key, (a, pmed, pmin, nmed, nmax, _b) in rep.items():
            v = "  强" if a >= 0.95 else ("  中" if a >= 0.8 else "  弱/无")
            print(f"    {key:<26}{a:>7.3f}{pmed:>9.2f}{pmin:>9.2f}"
                  f"{nmed:>9.2f}{nmax:>9.2f}{pmin - nmax:>8.2f}{v}")
            md.append(f"| {corpus} | {tag} | {key} | {a:.3f} | {pmed:.2f} | {nmed:.2f} |")

        print("  [B] 离线全库口径（参考；线上拿不到全库统计量，不可复现）")
        for key in off["pos"][0]:
            p = [r[key] for r in off["pos"]]
            n = [r[key] for r in off["neg"]]
            a = auc(p, n)
            print(f"    {key:<26}{a:>7.3f}{np.median(p):>9.2f}{np.median(n):>9.2f}")

        print(f"  [C] 阈值选点（{guard}，库内保留率优先）")
        for key, (a, _pm, _pmin, _nm, _nmax, best) in rep.items():
            if not best:
                print(f"    {key:<26}（该约束下无可选点）")
                continue
            print(f"    {key:<26}t={best['t']:.3f}  TPR={best['tpr']:.3f}  FPR={best['fpr']:.3f}"
                  f"  ｜ 库内留 {best['kept']}/误拦 {best['missed']}，"
                  f"库外拦 {best['blocked']}/漏过 {best['leaked']}")

        # [E] 复合规则对照：单信号失效时，AND 通常比 OR 保守（优先不误拦库内）
        ts = {key: rep[key][5] for key in rep}
        pos_rows, neg_rows = rows["pos"], rows["neg"]
        rules = []
        if ts.get("top1 绝对分") and ts.get("ratio = top1/mean(top_k)"):
            ta = ts["top1 绝对分"]["t"]
            tr = ts["ratio = top1/mean(top_k)"]["t"]
            rules = [
                (f"abs<{ta:.1f} 单独", lambda r: r["top1 绝对分"] < ta),
                (f"ratio<{tr:.3f} 单独", lambda r: r["ratio = top1/mean(top_k)"] < tr),
                ("AND: abs低 且 ratio低", lambda r: r["top1 绝对分"] < ta
                 and r["ratio = top1/mean(top_k)"] < tr),
                ("OR : abs低 或 ratio低", lambda r: r["top1 绝对分"] < ta
                 or r["ratio = top1/mean(top_k)"] < tr),
            ]
        if rules:
            print("  [E] 复合规则对照（阈值取 [C] 各单信号选点；拦截=判为「无检索支撑」）")
            for label, fn in rules:
                kept = sum(1 for r in pos_rows if not fn(r))
                blocked = sum(1 for r in neg_rows if fn(r))
                print(f"    {label:<24}库内保留 {kept/len(pos_rows)*100:>5.1f}%"
                      f"   库外拦截 {blocked/len(neg_rows)*100:>5.1f}%"
                      f"  ← {'推荐（保守）' if 'AND' in label else ''}")

        if args.natural:
            try:
                sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))
                import butler_fusion as _bf  # noqa: E402

                _abs_gate, _ratio_gate = _bf.ABS_GATE, _bf.RATIO_GATE
            except Exception:  # noqa: BLE001
                _abs_gate, _ratio_gate = 25.4, 1.14
            report_natural(idx, docs, k, _abs_gate, _ratio_gate)
            print()

        if args.show_leaks and ts.get("ratio = top1/mean(top_k)"):
            show_leaks(idx, docs, ts["ratio = top1/mean(top_k)"]["t"], k)
        print("=" * 88 + "\n")

    if args.markdown:
        head = ["| 语料 | 配置 | 门控信号 | AUC | 正例 med | 负例 med |", "|---|---|---|---|---|---|"]
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write("\n".join(head + md) + "\n")
        print(f"✔ markdown 表已写入 {args.markdown}")


if __name__ == "__main__":
    main()
