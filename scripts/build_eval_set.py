"""build_eval_set.py — 构造 RAG 检索评测集（可复现）

从 rag_data/docs.jsonl 抽取 N 个唯一标题作为 query，gold = 该标题对应的全部 chunk id。
固定随机种子（SEED=42），任何人 clone 后跑出的评测集逐字节一致。

两种难度档：
  easy（默认，零成本）
      直接用百科标题作 query。用途是**验证检索链路与索引配置差异**——
      标题与正文同源、词汇重叠高，绝对值偏高，不能当作真实泛化能力。
  hard（--llm）
      调 DeepSeek 把标题改写成口语化提问、刻意远离原文用词，测真实泛化。
      需要 DEEPSEEK_API_KEY；成本极低（200 条约几分钱）。

输出：rag_data/eval_set.jsonl
  每行 {"qid","query","gold_ids","difficulty","source_title"}

用法：
  python scripts/build_eval_set.py                        # 200 条 easy
  python scripts/build_eval_set.py --n 300                # 自定义体量
  python scripts/build_eval_set.py --llm --out-hard       # 额外产出一份 hard 集
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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS = os.path.join(_REPO_ROOT, "rag_data", "docs.jsonl")
_OUT = os.path.join(_REPO_ROOT, "rag_data", "eval_set.jsonl")
_OUT_HARD = os.path.join(_REPO_ROOT, "rag_data", "eval_set_hard.jsonl")

MIN_TITLE_CHARS = 6     # 过短标题歧义大，剔除
MIN_CONTENT_CHARS = 40  # 正文过短无检索价值
SEED = 42


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


def build_easy(groups: dict, docs: list[dict], n: int) -> list[dict]:
    random.seed(SEED)
    titles = sorted(groups.keys())          # 排序后再抽样，保证跨机器可复现
    picked = random.sample(titles, min(n, len(titles)))
    out = []
    for idx, title in enumerate(picked, 1):
        gold = sorted({docs[i]["id"] for i in groups[title]})
        out.append({
            "qid": f"e{idx:04d}",
            "query": title,
            "gold_ids": gold,
            "difficulty": "easy",
            "source_title": title,
        })
    return out


_PARAPHRASE_SYS = (
    "你是数据构造助手。把给定的百科标题改写成一个普通用户的自然提问。要求：\n"
    "1. 保留核心医学意图，不得改变主题或替换成别的疾病\n"
    "2. 换掉大部分用词，与原标题不要有明显字面重叠（不要照抄专有名词以外的词）\n"
    "3. 口语化，可以带一点模糊描述或症状主诉\n"
    "4. 只输出改写后的问句本身，不要引号、不要解释、不要编号"
)


def build_hard(easy: list[dict], model: str) -> list[dict]:
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

    out = []
    total = len(easy)
    for i, item in enumerate(easy, 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _PARAPHRASE_SYS},
                    {"role": "user", "content": f"原标题：{item['source_title']}"},
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
        out.append({
            "qid": f"h{i:04d}",
            "query": q,
            "gold_ids": item["gold_ids"],
            "difficulty": "hard",
            "source_title": item["source_title"],
        })
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
    ap.add_argument("--docs", default=_DOCS, help="语料 jsonl 路径")
    ap.add_argument("--out", default=_OUT, help="评测集输出路径（easy）")
    ap.add_argument("--n", type=int, default=200, help="评测集条数")
    ap.add_argument("--llm", action="store_true", help="额外产出 hard 档（需 DEEPSEEK_API_KEY）")
    ap.add_argument("--out-hard", action="store_true", help="hard 集写入 eval_set_hard.jsonl")
    ap.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    args = ap.parse_args()

    docs = load_docs(args.docs)
    groups = group_by_title(docs)
    print(f"语料 {len(docs)} chunks，可用标题 {len(groups)} 个", flush=True)

    easy = build_easy(groups, docs, args.n)
    _write(args.out, easy)

    if args.llm:
        _load_dotenv()
        print(f"▶ 调用 {args.model} 改写 {len(easy)} 条 query …", flush=True)
        hard = build_hard(easy, args.model)
        _write(_OUT_HARD if args.out_hard else args.out.replace(".jsonl", "_hard.jsonl"), hard)


if __name__ == "__main__":
    main()
