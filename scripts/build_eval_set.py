"""build_eval_set.py — 构造 RAG 检索评测集（可复现）

从指定语料抽取 query，并给出 gold chunk id 列表。固定随机种子（SEED=42），
任何人 clone 后跑出的评测集逐字节一致。

两种粒度（`--level`）
--------------------
- `doc`（默认）  query = 文档标题，gold = 该标题对应的全部 chunk。
  用途：**索引配置消融**（title 权重）、**跨语料/跨切片策略对照**（同一份 query 跑
  naive 与 section-aware 两份语料，看出"能不能找到正确文档"）。
  ⚠ 文档级 gold 集很大（IVD 语料每篇约 52 块），**Recall@k 不可解读**（上限≈k/|gold|），
  请只读 Hit@k 与 MRR@10。
- `section`      query = 「文档标题 + 章节末级名」，gold = 该 (doc_id, section) 的 chunk
  （中位数 1 块）。用途：**答案定位精度**——section-aware 切片的增益在这里才看得见，
  且 Recall@k 有意义（gold 小）。

两种难度档（`--llm`）
--------------------
  easy（默认，零成本）直接用原始标题/章节名作 query，测链路与配置差异（绝对值偏高）。
  hard（`--llm`）     调 DeepSeek 改写成口语化提问、刻意远离原文用词，测真实泛化。
                      需要 DEEPSEEK_API_KEY；成本极低（数百条约几分钱）。

输出：`rag_data/eval_set_*.jsonl`
  每行 {"qid","query","gold_ids","difficulty","source_title"[,"source_section"]}

用法：
  python scripts/build_eval_set.py                            # 默认语料(ivd) 文档级
  python scripts/build_eval_set.py --corpus general           # 通用对照域
  python scripts/build_eval_set.py --level section --n 800    # 章节级
  python scripts/build_eval_set.py --corpus ivd_naive --out rag_data/eval_set_ivd.jsonl
  python scripts/build_eval_set.py --llm --out-hard           # 额外产出 hard 档
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_OUT_DIR = os.path.join(_REPO_ROOT, "rag_data")

# 语料名 → (语料文件, 文档级评测集输出, hard 档输出)。与 app/rag_retriever._CORPORA 对齐。
_CORPUS_SPEC = {
    "ivd": ("docs_ivd_section.jsonl", "eval_set_ivd.jsonl", "eval_set_ivd_hard.jsonl"),
    "ivd_naive": ("docs_ivd_naive.jsonl", "eval_set_ivd_naive.jsonl", "eval_set_ivd_naive_hard.jsonl"),
    "general": ("docs_general.jsonl", "eval_set.jsonl", "eval_set_hard.jsonl"),
}

MIN_TITLE_CHARS = 6     # 过短标题歧义大，剔除
MIN_CONTENT_CHARS = 40  # 正文过短无检索价值
SEED = 42

# 章节级：章节末级名的可用长度区间；过短歧义、过长多半不是标题
SEC_LEAF_MIN, SEC_LEAF_MAX = 4, 40
SEC_MIN_CONTENT = 60    # 该章节至少要有一块正文超过此长度，否则无检索价值
SEC_MAX_GOLD = 8        # gold 过大的章节剔除，避免退化成文档级


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


def load_docs(path: str) -> list[dict]:
    docs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


def group_by_title(docs: list[dict]) -> dict[str, list]:
    """title -> [doc index]，过滤过短标题/正文。"""
    groups: dict[str, list] = defaultdict(list)
    for i, d in enumerate(docs):
        title = (d.get("title") or "").strip()
        content = (d.get("content") or "").strip()
        if len(title) < MIN_TITLE_CHARS or len(content) < MIN_CONTENT_CHARS:
            continue
        groups[title].append(i)
    return dict(groups)


def _usable_leaf(sec: str) -> str:
    """章节路径 → 末级名；不可用作 query 的章节返回空串。"""
    leaf = sec.split("·")[-1].strip()
    if not (SEC_LEAF_MIN <= len(leaf) <= SEC_LEAF_MAX):
        return ""
    if leaf.startswith(("附件", "附录", "指导原则", "参考文献")):
        return ""
    if "《" in leaf or "[" in leaf:      # 参考文献条目等
        return ""
    return leaf


def group_by_section(docs: list[dict]) -> dict[tuple, list]:
    """(doc_id, title, section, leaf) -> [doc index]，过滤不可用章节。"""
    groups: dict[tuple, list] = defaultdict(list)
    for i, d in enumerate(docs):
        sec = (d.get("section") or "").strip()
        doc_id = d.get("doc_id")
        if not sec or not doc_id:
            continue
        leaf = _usable_leaf(sec)
        if not leaf:
            continue
        groups[(doc_id, (d.get("title") or "").strip(), sec, leaf)].append(i)
    return dict(groups)


def build_doc(groups: dict, docs: list[dict], n: int) -> list[dict]:
    random.seed(SEED)
    titles = sorted(groups.keys())          # 排序后再抽样，保证跨机器可复现
    picked = random.sample(titles, min(n, len(titles)))
    out = []
    for idx, title in enumerate(picked, 1):
        gold = sorted({docs[i]["id"] for i in groups[title]})
        out.append({
            "qid": f"d{idx:04d}",
            "query": title,
            "gold_ids": gold,
            "difficulty": "easy",
            "level": "doc",
            "source_title": title,
        })
    return out


def build_section(groups: dict, docs: list[dict], n: int) -> list[dict]:
    """章节级：query = 「标题 章节末级名」，gold = 该章节的全部 chunk。"""
    usable = []
    for key, idxs in groups.items():
        contents = [(docs[i].get("content") or "") for i in idxs]
        if len(idxs) > SEC_MAX_GOLD:
            continue
        if max(len(c) for c in contents) < SEC_MIN_CONTENT:
            continue
        usable.append(key)
    usable.sort()
    random.seed(SEED)
    picked = random.sample(usable, min(n, len(usable)))
    out = []
    for idx, (doc_id, title, sec, leaf) in enumerate(picked, 1):
        out.append({
            "qid": f"s{idx:04d}",
            "query": f"{title} {leaf}",
            "gold_ids": sorted({docs[i]["id"] for i in groups[(doc_id, title, sec, leaf)]}),
            "difficulty": "easy",
            "level": "section",
            "source_title": title,
            "source_section": sec,
        })
    return out


def _paraphrase_sys(level: str, domain: str) -> str:
    if level == "section":
        topic = "「文档标题 + 章节名」"
        body = (
            "你是数据构造助手。给定一条「文档标题 + 章节名」，改写成工程师向该文档提的一个自然问题，"
            "要求：\n"
            "1. 保留原主题与章节指向，不得换成别的文档或别的章节\n"
            "2. 换掉大部分用词，不要照抄章节名以外的词\n"
            "3. 口语化，像在问「这份文件里关于 X 的部分怎么规定的 / 要求是什么」\n"
            "4. 只输出改写后的问句本身，不要引号、不要解释、不要编号"
        )
    else:
        topic = "文档标题"
        body = (
            f"你是数据构造助手。把给定的{domain}文档标题改写成一个普通用户会问的自然问题。要求：\n"
            "1. 保留核心主题，不得改变主题或替换成别的对象\n"
            "2. 换掉大部分用词，与原标题不要有明显字面重叠\n"
            "3. 口语化，可以带一点模糊描述或实际诉求\n"
            "4. 只输出改写后的问句本身，不要引号、不要解释、不要编号"
        )
    return body.format(topic=topic)


def build_hard(easy: list[dict], model: str, level: str, domain: str) -> list[dict]:
    """用 DeepSeek 把 easy query 改写成口语化问法，测真实泛化。"""
    from openai import OpenAI

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("✗ --llm 需要 DEEPSEEK_API_KEY（可写入项目根 .env）", flush=True)
        sys.exit(1)
    client = OpenAI(
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=key,
        timeout=30,
    )
    sys_prompt = _paraphrase_sys(level, domain)

    out = []
    total = len(easy)
    for i, item in enumerate(easy, 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"原文：{item['query']}"},
                ],
                temperature=0.8,
                max_tokens=60,
            )
            q = (resp.choices[0].message.content or "").strip().strip('"').strip()
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ [{i}/{total}] {type(e).__name__}: {e}", flush=True)
            q = ""
        if not q:
            continue
        rec = {
            "qid": item["qid"].replace("d", "h", 1) if item["qid"][0] == "d" else item["qid"].replace("s", "h", 1),
            "query": q,
            "gold_ids": item["gold_ids"],
            "difficulty": "hard",
            "level": item.get("level", "doc"),
            "source_title": item["source_title"],
            "original_query": item["query"],
        }
        if item.get("source_section"):
            rec["source_section"] = item["source_section"]
        out.append(rec)
        if i % 20 == 0 or i == total:
            print(f"  [{i}/{total}] 改写进度", flush=True)
    return out


def _write(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"✔ 写入 {path}（{len(rows)} 条）", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="构造 RAG 检索评测集")
    ap.add_argument("--corpus", default="ivd", choices=sorted(_CORPUS_SPEC),
                    help="语料名（与 app/rag_retriever._CORPORA 对齐）")
    ap.add_argument("--docs", default=None, help="语料 jsonl 路径（覆盖 --corpus）")
    ap.add_argument("--out", default=None, help="评测集输出路径（覆盖 --corpus 约定）")
    ap.add_argument("--level", default="doc", choices=["doc", "section"],
                    help="doc=标题级（消融/跨语料对照）；section=章节级（定位精度）")
    ap.add_argument("--n", type=int, default=200, help="评测集条数")
    ap.add_argument("--llm", action="store_true", help="额外产出 hard 档（需 DEEPSEEK_API_KEY）")
    ap.add_argument("--out-hard", action="store_true", help="hard 集写入约定的 *_hard.jsonl")
    ap.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    args = ap.parse_args()

    docs_file, out_default, out_hard = _CORPUS_SPEC[args.corpus]
    docs_path = args.docs or os.path.join(_OUT_DIR, docs_file)
    if args.out:
        out_path = args.out
    elif args.level == "section":
        out_path = os.path.join(_OUT_DIR, "eval_set_ivd_sec.jsonl")
    else:
        out_path = os.path.join(_OUT_DIR, out_default)

    if not os.path.exists(docs_path):
        print(f"✗ 语料不存在：{docs_path}", flush=True)
        sys.exit(1)

    docs = load_docs(docs_path)
    print(f"语料 {args.corpus}：{len(docs)} chunks（{os.path.basename(docs_path)}）", flush=True)

    if args.level == "section":
        groups = group_by_section(docs)
        n_sec = len(groups)
        rows = build_section(groups, docs, args.n)
        print(f"可用章节 {n_sec} 个 → 采样 {len(rows)} 条", flush=True)
    else:
        groups = group_by_title(docs)
        print(f"可用标题 {len(groups)} 个", flush=True)
        rows = build_doc(groups, docs, args.n)

    if rows:
        g = sorted(len(r["gold_ids"]) for r in rows)
        print(f"gold 集大小：min={g[0]} p50={g[len(g)//2]} max={g[-1]}", flush=True)
    _write(out_path, rows)

    if args.llm:
        _load_dotenv()
        domain = "体外诊断试剂法规" if args.corpus.startswith("ivd") else "医疗"
        print(f"▶ 调用 {args.model} 改写 {len(rows)} 条 query（level={args.level}）…", flush=True)
        hard = build_hard(rows, args.model, args.level, domain)
        target = (out_hard if args.out_hard
                  else out_path.replace(".jsonl", "_hard.jsonl"))
        _write(target, hard)


if __name__ == "__main__":
    main()
