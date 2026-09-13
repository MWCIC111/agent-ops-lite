"""RAG 检索层：从 rag_data/ 下选定语料构建离线 BM25 索引（jieba 分词）。

语料选择（`RAG_CORPUS`）
----------------------
| 取值 | 文件 | 说明 |
| --- | --- | --- |
| `ivd`（默认） | `rag_data/docs_ivd_section.jsonl` | **体外诊断试剂域**：NMPA 器审中心指导原则 172 篇，section-aware 切片（9004 chunks），带章节上下文前缀 |
| `general` | `rag_data/docs_general.jsonl` | 通用医疗对照域（原华佗百科抽样 5000 chunks），用于跨域对照 |
| 任意 `*.jsonl` 路径 | 该路径 | 评测对照用：如 `RAG_CORPUS=rag_data/docs_ivd_naive.jsonl` 跑朴素切分基线 |

语料字段：`{id, title, content, source, ...}`，IVD 语料额外带 `doc_id` / `section`。
来源与许可见 `rag_data/ivd_manifest.json`（逐篇标题 / 文号 / 器审中心原始发布页 URL）。

其它设计要点
------------
- 索引在首次调用时构建并缓存，无需重模型、无需联网。
- 索引字段 = content + title × TITLE_WEIGHT。标题信息密度高，实测把标题排除在索引外
  会让 Hit@5 从 1.000 掉到 0.485（scripts/eval_rag.py 可复现）。可用 `RAG_TITLE_WEIGHT=0` 退回纯 content。
- 人工审核回写（rag_data/reviewed.jsonl）并入同一索引，mtime 变化即自动重建，
  「回写 → 复问命中 → 置信度跳升」的闭环无需重启进程。
"""
from __future__ import annotations

import json
import os

from jieba import lcut
from rank_bm25 import BM25Okapi

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REVIEWED_PATH = os.path.join(_REPO_ROOT, "rag_data", "reviewed.jsonl")

# 命名语料 → 文件。按「域」而不是「版本」命名，便于 README / 评测脚本共享同一套真源。
_CORPORA = {
    "ivd": "docs_ivd_section.jsonl",          # 体外诊断试剂（NMPA 指导原则，section-aware）
    "ivd_naive": "docs_ivd_naive.jsonl",      # 同一批文档的朴素标点切分（消融基线）
    "general": "docs_general.jsonl",          # 通用医疗对照域（原华佗百科）
}
_DEFAULT_CORPUS = "ivd"

# 标题在索引中的重复次数（0 = 退回纯 content 索引，用于消融对照）。
# 5000 条通用语料实测：title×3 → Hit@5 1.000 / MRR@10 0.990；content-only 仅 0.485 / 0.380。
TITLE_WEIGHT = int(os.environ.get("RAG_TITLE_WEIGHT", "3"))

_cache: dict = {}


def corpus_name() -> str:
    """当前选定的语料名（env `RAG_CORPUS`，非法值回退默认并打印提示）。"""
    raw = (os.environ.get("RAG_CORPUS") or "").strip()
    if not raw:
        return _DEFAULT_CORPUS
    if raw in _CORPORA:
        return raw
    # 允许直接给 jsonl 路径（评测脚本用它跑任意对照语料）
    if raw.endswith(".jsonl"):
        return raw
    print(f"[rag_retriever] 未知 RAG_CORPUS={raw!r}，回退 {_DEFAULT_CORPUS}")
    return _DEFAULT_CORPUS


def corpus_path() -> str:
    """当前语料的绝对路径。**评测脚本的唯一真源**——避免两边各自写死路径而口径漂移。"""
    name = corpus_name()
    if name.endswith(".jsonl"):
        return name if os.path.isabs(name) else os.path.join(_REPO_ROOT, name)
    return os.path.join(_REPO_ROOT, "rag_data", _CORPORA[name])


# 语料名 → 人类可读标签（UI / Trace 展示用）。
# 放在数据源模块里作为**唯一真源**：换 RAG_CORPUS 时，页面文案与 Trace 工具名自动跟着变，
# 避免"数据源换了、展示层没跟"的漂移。
_CORPUS_LABEL = {
    "ivd": "IVD 器审指导原则",
    "ivd_naive": "IVD 器审指导原则（朴素切分）",
    "general": "华佗百科（对照域）",
}


def corpus_label(name: str | None = None) -> str:
    """当前（或指定）语料的人类可读名；未登记的取值原样返回（可能是 jsonl 路径）。"""
    name = name or corpus_name()
    return _CORPUS_LABEL.get(name, name)


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
    cname = corpus_name()
    if "index" not in _cache or _cache.get("_rev_mtime") != mtime or _cache.get("_corpus") != cname:
        docs = []
        path = corpus_path()
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"语料不存在：{path}\n"
                f"  RAG_CORPUS={cname}，可选：{', '.join(_CORPORA)}\n"
                f"  IVD 语料构建：python scripts/build_ivd_corpus.py --src <raw_md> --manifest <manifest.json>"
            )
        with open(path, "r", encoding="utf-8") as f:
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
        # 标题重复 TITLE_WEIGHT 次以提升其权重；标题缺失（如人工回写条目）时该项为空
        corpus = [
            lcut(d["content"]) + lcut(d.get("title", "")) * TITLE_WEIGHT
            for d in docs
        ]
        _cache["docs"] = docs
        _cache["index"] = BM25Okapi(corpus)
        _cache["size"] = len(docs)
        _cache["_rev_mtime"] = mtime
        _cache["_corpus"] = cname
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
