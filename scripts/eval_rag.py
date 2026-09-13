"""eval_rag.py — RAG 检索效果评测（Hit@k / Recall@k / MRR）

在固定评测集上对比多种索引配置，输出可直接贴进 README 的对比表。

基准口径：`--sweep 0`（title 重复 0 次 = 纯 content 索引）作历史对照；线上现行配置由
`app/rag_retriever.py` 的 `TITLE_WEIGHT` 决定（默认 3）。`--verify-online` 会读取该常量、
在本地复现同一索引逐条比对 top1，因此线上配置变更后本校验依然自洽（不会假阳性通过）。

用法：
  python scripts/eval_rag.py                                  # 默认对比 w=0/1/3/5
  python scripts/eval_rag.py --eval-set rag_data/eval_set_hard.jsonl
  python scripts/eval_rag.py --verify-online                  # 校验 baseline == 线上
  python scripts/eval_rag.py --markdown eval_report.md        # 导出 markdown 表
  python scripts/eval_rag.py --show-fails 5                   # 看检索失败的 query
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from jieba import lcut
from rank_bm25 import BM25Okapi

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS = os.path.join(_REPO_ROOT, "rag_data", "docs.jsonl")
_EVAL = os.path.join(_REPO_ROOT, "rag_data", "eval_set.jsonl")


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def evaluate(index, docs, samples, ks, mrr_k=10):
    """返回 {hit, recall, mrr, n, fails}。gold_ids 多块时 Recall 有意义。"""
    n = len(samples)
    max_k = max(max(ks), mrr_k)
    hits = {k: 0 for k in ks}
    recall = {k: 0.0 for k in ks}
    mrr = 0.0
    fails = []

    for s in samples:
        gold = set(s["gold_ids"])
        scores = np.asarray(index.get_scores(lcut(s["query"])), dtype=float)
        order = np.argsort(-scores)[:max_k]
        ids = [docs[i]["id"] for i in order]

        hit_any = False
        for k in ks:
            got = sum(1 for x in ids[:k] if x in gold)
            if got:
                hits[k] += 1
                hit_any = True
            recall[k] += got / len(gold)
        for rank, x in enumerate(ids[:mrr_k], 1):
            if x in gold:
                mrr += 1.0 / rank
                break
        if not hit_any:
            fails.append(s)

    return {
        "n": n,
        "hit": {k: hits[k] / n for k in ks},
        "recall": {k: recall[k] / n for k in ks},
        "mrr": mrr / n,
        "fails": fails,
    }


def _online_title_weight() -> int:
    """线上 app/rag_retriever 当前使用的 title 权重（以线上代码为唯一真源）。"""
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))
        import rag_retriever  # noqa: E402

        return int(getattr(rag_retriever, "TITLE_WEIGHT", 0))
    except Exception:  # noqa: BLE001
        return 0


def _label(w: int, online_w: int) -> str:
    name = f"title×{w} + content" if w else "content-only"
    if w == online_w:
        return f"{name}（线上现行）"
    return f"{name}（历史对照）"


def _build_results(docs, samples, weights, ks):
    """分词只做一次，各权重配置复用 token 序列，避免重复分词开销。"""
    online_w = _online_title_weight()
    t0 = time.time()
    content_toks = [lcut(d["content"]) for d in docs]
    title_toks = [lcut(d["title"]) for d in docs]
    print(f"分词完成（{len(docs)} chunks，{time.time()-t0:.1f}s）", flush=True)

    results = []
    for w in weights:
        corpus = [
            content_toks[i] + title_toks[i] * w for i in range(len(docs))
        ]
        idx = BM25Okapi(corpus)
        r = evaluate(idx, docs, samples, ks)
        r["w"] = w
        r["label"] = _label(w, online_w)
        results.append(r)
        print(f"  w={w} 评测完成  Hit@5={r['hit'].get(5, float('nan')):.3f}", flush=True)
    return results


def _markdown(results, ks) -> str:
    base = results[0]
    head = "| 配置 | " + " | ".join(f"Hit@{k}" for k in ks) + " | Recall@5 | MRR@10 |"
    sep = "|" + "---|" * (len(ks) + 3)
    lines = [head, sep]
    for r in results:
        cells = [f"**{r['label']}**" if r is base else r["label"]]
        for k in ks:
            v = r["hit"][k]
            if r is not base:
                d = (v - base["hit"][k]) * 100
                cells.append(f"{v:.3f} ({d:+.1f}pp)")
            else:
                cells.append(f"{v:.3f}")
        r5 = r["recall"].get(5, 0.0)
        if r is not base:
            r5 = f"{r5:.3f} ({(r5-base['recall'].get(5,0.0))*100:+.1f}pp)"
        else:
            r5 = f"{r5:.3f}"
        cells.append(r5)
        m = r["mrr"]
        cells.append(f"{m:.3f}" if r is base else f"{m:.3f} ({(m-base['mrr'])*100:+.1f}pp)")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def verify_online(docs, samples, n=10) -> bool:
    """校验「线上现行配置」与 app/rag_retriever 实际返回的 top1 文档是否一致。

    真源是线上代码本身：从 rag_retriever.TITLE_WEIGHT 读权重后在本地复现同一索引，
    因此线上换了索引配置后本校验依然有效，不会因为"两边都写死 w=0"而假通过。
    """
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))
        import rag_retriever  # noqa: E402
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 无法导入 app/rag_retriever（{type(e).__name__}: {e}），跳过校验")
        return False

    w = int(getattr(rag_retriever, "TITLE_WEIGHT", 0))
    # 线上 _load() 会把 rag_data/reviewed.jsonl（人工回写知识）并入同一索引，
    # 这里必须复现同一份语料，否则线上多出的回写条目会造成假不一致（实测 3/10）。
    corpus_docs = list(docs)
    reviewed = os.path.join(_REPO_ROOT, "rag_data", "reviewed.jsonl")
    if os.path.exists(reviewed):
        corpus_docs += load_jsonl(reviewed)
    corpus = [lcut(d["content"]) + lcut(d.get("title", "")) * w for d in corpus_docs]
    idx = BM25Okapi(corpus)
    ok = 0
    for s in samples[:n]:
        scores = np.asarray(idx.get_scores(lcut(s["query"])), dtype=float)
        top = docs[int(np.argmax(scores))]["content"]
        online = rag_retriever.retrieve(s["query"], top_k=1)
        if online and online[0]["content"] == top:
            ok += 1
    n_eff = min(n, len(samples))
    print(f"线上口径校验（TITLE_WEIGHT={w}）：{ok}/{n_eff} 条 top1 一致"
          f"{'  ✔ 通过' if ok == n_eff else '  ✗ 不一致，需排查'}")
    return ok == n_eff


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG 检索效果评测")
    ap.add_argument("--docs", default=_DOCS, help="语料 jsonl")
    ap.add_argument("--eval-set", default=_EVAL, help="评测集 jsonl")
    ap.add_argument("--sweep", default="0,1,3,5", help="title 重复次数，逗号分隔；0=线上现状")
    ap.add_argument("--ks", default="1,3,5,10", help="Hit@k / Recall@k 的 k 列表")
    ap.add_argument("--markdown", default="", help="把对比表写到该 markdown 文件")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")
    ap.add_argument("--show-fails", type=int, default=0, help="打印 baseline 检索失败的 query")
    ap.add_argument("--verify-online", action="store_true", help="校验 baseline 与线上实现一致")
    args = ap.parse_args()

    if not os.path.exists(args.eval_set):
        print(f"✗ 评测集不存在：{args.eval_set}\n  先跑：python scripts/build_eval_set.py")
        sys.exit(1)

    docs = load_jsonl(args.docs)
    samples = load_jsonl(args.eval_set)
    weights = [int(x) for x in args.sweep.split(",") if x.strip()]
    ks = [int(x) for x in args.ks.split(",") if x.strip()]

    diff = samples[0].get("difficulty", "?") if samples else "?"
    print(f"语料 {len(docs)} chunks ｜ 评测集 {len(samples)} 条（difficulty={diff}）\n")

    if args.verify_online:
        verify_online(docs, samples)
        print()

    results = _build_results(docs, samples, weights, ks)
    table = _markdown(results, ks)

    print("\n" + table + "\n")
    best = max(results, key=lambda r: r["hit"].get(5, 0))
    base = results[0]
    if best is not base:
        print(f"最优配置：{best['label']}（Hit@5 {best['hit'].get(5,0):.3f}，"
              f"相对 baseline {(best['hit'].get(5,0)-base['hit'].get(5,0))*100:+.1f}pp）")
    else:
        print(f"当前 baseline 已是最优：{base['label']}")

    if args.show_fails:
        print(f"\nbaseline 检索失败样例（前 {args.show_fails} 条）：")
        for s in base["fails"][: args.show_fails]:
            print(f"  - [{s['qid']}] {s['query']}")

    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(table + "\n")
        print(f"\n✔ markdown 表已写入 {args.markdown}")

    if args.json:
        print(json.dumps(
            [{"config": r["label"], "w": r["w"],
              "hit": {str(k): round(v, 4) for k, v in r["hit"].items()},
              "recall": {str(k): round(v, 4) for k, v in r["recall"].items()},
              "mrr": round(r["mrr"], 4), "n": r["n"]} for r in results],
            ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
