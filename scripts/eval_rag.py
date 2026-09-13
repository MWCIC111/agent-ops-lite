"""eval_rag.py — RAG 检索效果评测（Hit@k / Recall@k / MRR）

在固定评测集上对比多种索引配置或多种语料，输出可直接贴进 README 的对比表。

语料与口径
----------
语料由 `--corpus` 选择（与 `app/rag_retriever._CORPORA` 对齐：ivd / ivd_naive / general），
默认取**线上当前语料**——即直接读 `app/rag_retriever.corpus_path()`，避免评测侧与线上
各写一份路径导致口径漂移。线上现行 title 权重同样从 `rag_retriever.TITLE_WEIGHT` 读，
`--verify-online` 会在本地复现同一索引逐条比对 top1（语料与线上不一致时会明确跳过校验，
不会假阳性通过）。

⚠ 文档级评测集（`--level doc` 造出的）gold 集很大（IVD 语料每篇约 52 块），
**Recall@k 不可解读**（上限≈k/|gold|）；请以 Hit@k 与 MRR@10 为准。
章节级评测集（`eval_set_ivd_sec.jsonl`，gold 中位数 1 块）的 Recall@k 才有意义。

用法：
  python scripts/eval_rag.py                                        # 线上语料
  python scripts/eval_rag.py --corpus ivd_naive --label 朴素切分基线
  python scripts/eval_rag.py --eval-set rag_data/eval_set_ivd_sec.jsonl --sweep 3
  python scripts/eval_rag.py --verify-online --markdown eval_report.md
  python scripts/eval_rag.py --show-fails 5                         # 看检索失败的 query
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_OUT_DIR = os.path.join(_REPO_ROOT, "rag_data")

# 语料名 → 默认的文档级评测集（与 scripts/build_eval_set.py::_CORPUS_SPEC 对齐）
_EVAL_SPEC = {
    "ivd": "eval_set_ivd.jsonl",
    "ivd_naive": "eval_set_ivd_naive.jsonl",
    "general": "eval_set.jsonl",
}


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _import_retriever():
    sys.path.insert(0, os.path.join(_REPO_ROOT, "app"))
    import rag_retriever  # noqa: E402

    return rag_retriever


def evaluate(index, docs, samples, ks, mrr_k=10):
    """返回 {hit, recall, mrr, n, fails, gold_p50}。gold_ids 多块时 Recall 才有意义。"""
    n = len(samples)
    max_k = max(max(ks), mrr_k)
    hits = {k: 0 for k in ks}
    recall = {k: 0.0 for k in ks}
    mrr = 0.0
    gold_sizes = []
    fails = []

    for s in samples:
        gold = set(s["gold_ids"])
        gold_sizes.append(len(gold))
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

    gold_sizes.sort()
    return {
        "n": n,
        "hit": {k: hits[k] / n for k in ks},
        "recall": {k: recall[k] / n for k in ks},
        "mrr": mrr / n,
        "gold_p50": gold_sizes[len(gold_sizes) // 2] if gold_sizes else 0,
        "fails": fails,
    }


def _label(w: int, online_w: int, tag: str) -> str:
    name = f"title×{w} + content" if w else "content-only"
    prefix = f"{tag} ｜ " if tag else ""
    mark = "（线上现行）" if w == online_w else "（历史对照）"
    return f"{prefix}{name}{mark}"


def _build_results(docs, samples, weights, ks, tag):
    """分词只做一次，各权重配置复用 token 序列，避免重复分词开销。"""
    online_w = int(getattr(_import_retriever(), "TITLE_WEIGHT", 0))
    t0 = time.time()
    content_toks = [lcut(d["content"]) for d in docs]
    title_toks = [lcut(d.get("title", "")) for d in docs]
    print(f"分词完成（{len(docs)} chunks，{time.time()-t0:.1f}s）", flush=True)

    results = []
    for w in weights:
        corpus = [
            content_toks[i] + title_toks[i] * w for i in range(len(docs))
        ]
        idx = BM25Okapi(corpus)
        r = evaluate(idx, docs, samples, ks)
        r["w"] = w
        r["label"] = _label(w, online_w, tag)
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


def verify_online(docs, docs_path, samples, n=10):
    """校验本评测的语料配置与 app/rag_retriever 实际行为是否一致。

    真源是线上代码本身：从 `rag_retriever.corpus_path()` 取语料、`TITLE_WEIGHT` 取权重，
    在本地复现同一索引逐条比对 top1。因此线上换语料或换权重后本校验依然有效。
    若本次评测用的语料不是线上当前语料（如对照跑 naive 基线），明确跳过而非误报不一致。
    """
    try:
        rr = _import_retriever()
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 无法导入 app/rag_retriever（{type(e).__name__}: {e}），跳过校验")
        return None

    online_path = os.path.abspath(rr.corpus_path())
    if os.path.abspath(docs_path) != online_path:
        print(f"⚠ 本次语料（{os.path.basename(docs_path)}）≠ 线上当前语料"
              f"（{os.path.basename(online_path)}），跳过线上校验")
        return None

    w = int(getattr(rr, "TITLE_WEIGHT", 0))
    # 线上 _load() 会把 rag_data/reviewed.jsonl（人工回写知识）并入同一索引，
    # 这里必须复现同一份语料，否则线上多出的回写条目会造成假不一致。
    corpus_docs = list(docs)
    reviewed = os.path.join(_OUT_DIR, "reviewed.jsonl")
    if os.path.exists(reviewed):
        corpus_docs += load_jsonl(reviewed)
    corpus = [lcut(d["content"]) + lcut(d.get("title", "")) * w for d in corpus_docs]
    idx = BM25Okapi(corpus)
    ok = 0
    for s in samples[:n]:
        scores = np.asarray(idx.get_scores(lcut(s["query"])), dtype=float)
        top = docs[int(np.argmax(scores))]["content"]
        online = rr.retrieve(s["query"], top_k=1)
        if online and online[0]["content"] == top:
            ok += 1
    n_eff = min(n, len(samples))
    print(f"线上口径校验（语料={rr.corpus_name()}，TITLE_WEIGHT={w}）："
          f"{ok}/{n_eff} 条 top1 一致"
          f"{'  ✔ 通过' if ok == n_eff else '  ✗ 不一致，需排查'}")
    return ok == n_eff


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG 检索效果评测")
    ap.add_argument("--corpus", default=None,
                    help="语料名（ivd / ivd_naive / general）；默认=线上当前语料")
    ap.add_argument("--docs", default=None, help="语料 jsonl 路径（覆盖 --corpus）")
    ap.add_argument("--eval-set", default=None, help="评测集 jsonl（默认随 --corpus 推导）")
    ap.add_argument("--sweep", default="0,1,3,5", help="title 重复次数，逗号分隔")
    ap.add_argument("--ks", default="1,3,5,10", help="Hit@k / Recall@k 的 k 列表")
    ap.add_argument("--label", default="", help="本次运行的标签，写进表格首列（多语料对照用）")
    ap.add_argument("--markdown", default="", help="把对比表写到该 markdown 文件")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")
    ap.add_argument("--show-fails", type=int, default=0, help="打印 baseline 检索失败的 query")
    ap.add_argument("--verify-online", action="store_true", help="校验本配置与线上实现一致")
    args = ap.parse_args()

    # 语料解析：显式 --corpus 要**先于**导入 rag_retriever 设置，保证 TITLE_WEIGHT/路径同源
    if args.corpus:
        os.environ["RAG_CORPUS"] = args.corpus
    rr = _import_retriever()
    corpus = args.corpus or rr.corpus_name()
    docs_path = args.docs or rr.corpus_path()
    eval_path = args.eval_set or os.path.join(
        _OUT_DIR, _EVAL_SPEC.get(corpus, "eval_set_ivd.jsonl"))

    if not os.path.exists(docs_path):
        print(f"✗ 语料不存在：{docs_path}", flush=True)
        sys.exit(1)
    if not os.path.exists(eval_path):
        print(f"✗ 评测集不存在：{eval_path}\n"
              f"  先跑：python scripts/build_eval_set.py --corpus {corpus}", flush=True)
        sys.exit(1)

    docs = load_jsonl(docs_path)
    samples = load_jsonl(eval_path)
    weights = [int(x) for x in args.sweep.split(",") if x.strip()]
    ks = [int(x) for x in args.ks.split(",") if x.strip()]

    diff = samples[0].get("difficulty", "?") if samples else "?"
    level = samples[0].get("level", "doc") if samples else "doc"
    print(f"语料 {corpus}：{len(docs)} chunks（{os.path.basename(docs_path)}）"
          f" ｜ 评测集 {len(samples)} 条（{os.path.basename(eval_path)}，"
          f"level={level}，difficulty={diff}）\n")
    if level == "doc":
        print("⚠ 文档级评测集 gold 集很大，Recall@k 不可解读（上限≈k/|gold|），请读 Hit@k / MRR@10\n")

    if args.verify_online:
        verify_online(docs, docs_path, samples)
        print()

    results = _build_results(docs, samples, weights, ks, args.label)
    table = _markdown(results, ks)

    print("\n" + table + "\n")
    print(f"gold 集大小中位数：{results[0]['gold_p50']} 块\n")
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
            [{"corpus": corpus, "config": r["label"], "w": r["w"],
              "hit": {str(k): round(v, 4) for k, v in r["hit"].items()},
              "recall": {str(k): round(v, 4) for k, v in r["recall"].items()},
              "mrr": round(r["mrr"], 4), "n": r["n"]} for r in results],
            ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
