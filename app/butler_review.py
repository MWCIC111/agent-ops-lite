"""butler_review.py — 人工审核回写闭环（高容错核心）

低置信回答不硬编，落入审核队列；人工审核通过 → 写回检索库（reviewed.jsonl），
下次同类问题即可被 BM25 命中，实现"越用越准"的闭环。

存储：agent_ops.db 内独立表 butler_reviews（与 Trace 同库，零额外依赖）。
回写目标：rag_data/reviewed.jsonl（rag_retriever 已加载该文件）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_REPO_ROOT, "agent_ops.db")
REVIEWED_PATH = os.path.join(_REPO_ROOT, "rag_data", "reviewed.jsonl")
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _init() -> None:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS butler_reviews (
                    id          TEXT PRIMARY KEY,
                    query       TEXT NOT NULL,
                    draft       TEXT,
                    confidence  REAL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    answer      TEXT,
                    created_at  TEXT,
                    reviewed_at TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def add(query: str, draft: str, confidence: float) -> str:
    """低置信回答入队，返回 review id。"""
    _init()
    rid = "rv_" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "INSERT INTO butler_reviews "
                "(id, query, draft, confidence, status, answer, created_at, reviewed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rid, query, draft, confidence, "pending", "", _now(), ""),
            )
            conn.commit()
        finally:
            conn.close()
    return rid


def pending() -> list:
    """返回待审核列表（dict）。"""
    _init()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM butler_reviews WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def approve(rid: str, answer: str) -> bool:
    """审核通过 → 更新状态 + 写回检索库。返回是否成功。"""
    _init()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT query FROM butler_reviews WHERE id=?", (rid,)
            ).fetchone()
            if row is None:
                return False
            query = row[0]
            conn.execute(
                "UPDATE butler_reviews SET status='approved', answer=?, reviewed_at=? "
                "WHERE id=?",
                (answer, _now(), rid),
            )
            conn.commit()
        finally:
            conn.close()
    # 写回检索库（追加一行，rag_retriever 启动时已加载）
    os.makedirs(os.path.dirname(REVIEWED_PATH), exist_ok=True)
    with _lock:
        with open(REVIEWED_PATH, "a", encoding="utf-8") as f:
            rec = {
                "title": "人工审核补充",
                "content": f"问：{query}\n答：{answer}",
                "source": "人工回写",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True
