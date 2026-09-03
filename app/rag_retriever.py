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
_REVIEWED_PATH = os.path.join(_REPO_ROOT, "rag_data", "reviewed.jsonl")

_cache: dict = {}


def _reviewed_mtime() -> float | None:
    """reviewed.jsonl 的 mtime（不存在返回 None）。用作缓存失效探针。"""
    try:
        return os.path.getmtime(_REVIEWED_PATH) if os.path.exists(_REVIEWED_PATH) else None
    except OSError:
        return None


def _load():
    """Load docs + build BM25 index (lazy, cached in-process, auto-refresh).

    额外加载 rag_data/reviewed.jsonl（人工审核回写闭环产出的知识），
    使审核通过后的答案可被 BM25 召回，实现"越用越准"。

    缓存失效：索引构建后记录 reviewed.jsonl 的 mtime；每次调用探测一次
    （os.path.getmtime 为纳秒级系统调用，开销可忽略）。审核通过追加一行
    → mtime 变化 → 下次检索自动重建索引 → "回写 → 复问命中 → 置信度跳升"
    的闭环在单次进程内即可演示，无需重启。
    """
    mtime = _reviewed_mtime()
    if "index" not in _cache or _cache.get("_rev_mtime") != mtime:
        docs = []
        with open(_DOCS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    docs.append(json.loads(line))
        # 人工回写知识（若存在）
        if os.path.exists(_REVIEWED_PATH):
            with open(_REVIEWED_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            docs.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        corpus = [lcut(d["content"]) for d in docs]
        _cache["docs"] = docs
        _cache["index"] = BM25Okapi(corpus)
        _cache["size"] = len(docs)
        _cache["_rev_mtime"] = mtime
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
