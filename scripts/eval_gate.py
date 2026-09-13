"""eval_gate.py — 置信度门控校准与可分性评测（AUC + 阈值选点）

问题背景（本脚本存在的理由）：
  butler_fusion 原先用 sigmoid(top_bm25 / 3.0) 计算「相似度分量」。但 rank_bm25 的
  BM25Okapi 原始分实测在 10~120 量级，分母 3.0 让分量恒饱和在 ~1.0 —— 库内（可答）
  与库外（不可答）问题拿到几乎相同的分量，AUC≈0.52，等于抛硬币；配套的
  「top_bm25 <= 0 则强制低置信」又因 BM25 分数恒为正而永不触发（死代码，实测 0/20）。
  结论：三层幻觉抑制里，**检索层的数值门控实际没有生效**，库外拦截全靠 LLM 自评那一层。

本脚本做三件事：
  [A] 在**线上真正可得的信号**上算 AUC —— 线上 retrieve(top_k=4) 只返回 4 个分数，
      所以归一化只能用这 4 个的内部统计量（这是与「离线全库口径」的关键区别）；
  [B] 给出离线全库口径作参考，并显式标注它线上复现不了（防止把离线数字误当线上能力）；
  [C] 在 FPR 约束下扫阈值，输出 TPR/FPR 与混淆计数，作为 butler_fusion.RATIO_GATE 的校准依据。

正例 = rag_data/eval_set.jsonl 的 query（库内有答案）
负例 = 内置库外问题集（IVD 研发 + 通用闲聊，语料中无对应答案）

用法：
  python scripts/eval_gate.py                                  # 默认对比 title 权重 0 / 3
  python scripts/eval_gate.py --sweep 0,1,3,5 --top-k 4
  python scripts/eval_gate.py --max-fpr 0.05                   # 更严的误拦约束
  python scripts/eval_gate.py --markdown gate_report.md        # 导出 markdown
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from jieba import lcut
from rank_bm25 import BM25Okapi

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS = os.path.join(_REPO_ROOT, "rag_data", "docs.jsonl")
_EVAL = os.path.join(_REPO_ROOT, "rag_data", "eval_set.jsonl")

# 库外问题：IVD/研发场景（与语料有医学词汇重叠、最易误命中）+ 通用闲聊
NEG_QUERIES = [
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
    "今天武汉天气怎么样？",
    "帮我写一首关于秋天的诗。",
    "Python 的 GIL 是什么？",
    "如何用 Docker 部署 FastAPI 服务？",
    "给我讲讲 LangGraph 的 Send 机制。",
]


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


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


def _row(name: str, feature: str, p, n, width: int = 26) -> str:
    a = auc(p, n)
    return (f"    {name:<{width}}{a:>7.3f}"
            f"{np.median(p):>9.2f}{np.median(n):>9.2f}"
            f"{'  强' if a >= 0.95 else ('  中' if a >= 0.8 else '  弱/无')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="置信度门控校准与可分性评测")
    ap.add_argument("--docs", default=_DOCS)
    ap.add_argument("--eval-set", default=_EVAL)
    ap.add_argument("--sweep", default="0,3", help="title 重复次数；线下与线上配置对比")
    ap.add_argument("--top-k", type=int, default=4, help="线上 retrieve 的 top_k（默认 4）")
    ap.add_argument("--max-fpr", type=float, default=0.10, help="阈值选点的 FPR 上限")
    ap.add_argument("--markdown", default="", help="把结果表写到该 markdown 文件")
    args = ap.parse_args()

    if not os.path.exists(args.eval_set):
        print(f"✗ 评测集不存在：{args.eval_set}\n  先跑：python scripts/build_eval_set.py")
        sys.exit(1)

    online_w = 0
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))
        import rag_retriever  # noqa: E402

        online_w = int(getattr(rag_retriever, "TITLE_WEIGHT", 0))
    except Exception:  # noqa: BLE001
        pass

    docs = load_jsonl(args.docs)
    samples = load_jsonl(args.eval_set)
    weights = [int(x) for x in args.sweep.split(",") if x.strip()]

    content_toks = [lcut(d["content"]) for d in docs]
    title_toks = [lcut(d.get("title", "")) for d in docs]

    print(f"语料 {len(docs)} chunks ｜ 正例（库内）{len(samples)} ｜ "
          f"负例（库外）{len(NEG_QUERIES)} ｜ 线上 TITLE_WEIGHT={online_w} ｜ top_k={args.top_k}\n")

    md: list[str] = []
    guard = f"FPR≤{args.max_fpr:.2f}"

    for w in weights:
        corpus = [content_toks[i] + title_toks[i] * w for i in range(len(docs))]
        idx = BM25Okapi(corpus)
        k = args.top_k
        rows = {
            "pos": [online_features(idx.get_scores(lcut(s["query"])), k) for s in samples],
            "neg": [online_features(idx.get_scores(lcut(q)), k) for q in NEG_QUERIES],
        }
        off = {
            "pos": [offline_features(idx.get_scores(lcut(s["query"]))) for s in samples],
            "neg": [offline_features(idx.get_scores(lcut(q))) for q in NEG_QUERIES],
        }
        name = "content-only" if w == 0 else f"title×{w} + content"
        tag = f"{name}（线上现行）" if w == online_w else name
        print(f"=== {tag} ===")

        print(f"  [A] 线上可得信号（仅 top_k={k} 内部统计量，线上可复现）")
        print(f"    {'信号':<26}{'AUC':>7}{'正例med':>9}{'负例med':>9}   区分力")
        print("    " + "-" * 62)
        for key in rows["pos"][0]:
            p = [r[key] for r in rows["pos"]]
            n = [r[key] for r in rows["neg"]]
            print(_row(key, key, p, n))
            md.append(f"| {tag} | {key} | {auc(p, n):.3f} | {np.median(p):.2f} | {np.median(n):.2f} |")

        print(f"  [B] 离线全库口径（参考；线上拿不到全库统计量，不可复现）")
        for key in off["pos"][0]:
            p = [r[key] for r in off["pos"]]
            n = [r[key] for r in off["neg"]]
            print(_row(key, key, p, n))

        print(f"  [C] 阈值选点（{guard}，库内保留率优先）")
        for key in rows["pos"][0]:
            p = [r[key] for r in rows["pos"]]
            n = [r[key] for r in rows["neg"]]
            best = pick_threshold(p, n, args.max_fpr)
            if not best:
                print(f"    {key:<26}（该约束下无可选点）")
                continue
            print(f"    {key:<26}t={best['t']:.3f}  TPR={best['tpr']:.3f}  FPR={best['fpr']:.3f}"
                  f"  ｜ 库内留 {best['kept']}/误拦 {best['missed']}，"
                  f"库外拦 {best['blocked']}/漏过 {best['leaked']}")
        print("=" * 78 + "\n")

    if args.markdown:
        head = ["| 配置 | 门控信号 | AUC | 正例 med | 负例 med |", "|---|---|---|---|---|"]
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write("\n".join(head + md) + "\n")
        print(f"✔ markdown 表已写入 {args.markdown}")


if __name__ == "__main__":
    main()
