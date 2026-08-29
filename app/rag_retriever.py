"""RAG 检索层：从 rag_data/docs.jsonl 构建离线 BM25 索引（jieba 分词）。

设计要点：
- 知识库来自 ModelScope 华佗百科（医疗/IVD 友好），离线抽样 5000 段。
- 索引在首次调用时构建并缓存，无需重模型、无需联网。
- 仅在用户主动检索时调用，纯文本检索，契合「涨红跌绿」等中文语境无关。
"""
from __future__ import annotations

import json
import os

from jieba import lcut
from rank_bm25 import BM25Okapi

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS_PATH = os.path.join(_REPO_ROOT, "rag_data", "docs.jsonl")

_cache: dict = {}


def _load():
    """Load docs + build BM25 index once (lazy, cached in-process)."""
    if "index" not in _cache:
        docs = []
        with open(_DOCS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    docs.append(json.loads(line))
        corpus = [lcut(d["content"]) for d in docs]
        _cache["docs"] = docs
        _cache["index"] = BM25Okapi(corpus)
        _cache["size"] = len(docs)
    return _cache["docs"], _cache["index"], _cache["size"]


def retrieve(query: str, top_k: int = 3) -> list[dict]:
    """Return top_k chunks dicts: {title, content, source, score}."""
    docs, index, _ = _load()
    q = lcut(query)
    scores = index.get_scores(q)
    top = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    return [
        {
            "title": docs[i]["title"],
            "content": docs[i]["content"],
            "source": docs[i].get("source", ""),
            "score": float(scores[i]),
        }
        for i in top
    ]


def count() -> int:
    try:
        _, _, size = _load()
        return size
    except Exception:
        return 0
