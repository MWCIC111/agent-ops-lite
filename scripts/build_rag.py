"""Build a Chinese RAG knowledge base from the cached Huatuo Encyclopedia dataset.

Source: ModelScope FreedomIntelligence/huatuo_encyclopedia_qa (medical/IVD-friendly).
We read the already-downloaded raw jsonl from the ModelScope cache, sample + chunk it,
and emit docs.jsonl. The BM25 index is rebuilt at app-load time from docs.jsonl via
jieba + rank_bm25, so the demo ships only text and runs without any heavy model on the
2C2G server.

Output:
  rag_data/docs.jsonl   - one JSON object per chunk: {id, title, content, source}
"""
import json
import os
import re
import random
from jieba import lcut

CACHE_FILE = (r"C:/Users/21561/.cache/modelscope/hub/datasets/downloads/"
              r"45e1c912258b5263e6d1647202e7a04abda91970b25cfcda43b9ace6130d1547")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rag_data")
DOCS_PATH = os.path.join(OUT_DIR, "docs.jsonl")

K = 5000          # target number of chunks retained (reservoir sampling)
MIN_ANS_LEN = 80  # drop answers shorter than this
CHUNK_SIZE = 350  # max chars per chunk for long answers

SPLIT_RE = re.compile(r"[。；;！!？?\n]")


def chunk_text(text: str):
    """Split a long answer into <=CHUNK_SIZE char pieces with light overlap."""
    text = text.strip()
    if len(text) <= CHUNK_SIZE:
        return [text]
    parts = [p for p in SPLIT_RE.split(text) if p.strip()]
    chunks, buf = [], ""
    for p in parts:
        if len(buf) + len(p) < CHUNK_SIZE:
            buf += p + "。"
        else:
            if buf:
                chunks.append(buf.strip())
            buf = p + "。"
    if buf:
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) >= 20]


def main():
    random.seed(42)
    reservoir = []
    count = 0
    total_lines = 0
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                obj = json.loads(line)
            except Exception:
                continue
            q = (obj.get("questions") or [[""]])[0][0]
            a = (obj.get("answers") or [""])[0]
            if not a or len(a) < MIN_ANS_LEN:
                continue
            for c in chunk_text(a):
                item = {"id": count, "title": q.strip(), "content": c,
                        "source": "华佗百科 (huatuo_encyclopedia_qa)"}
                count += 1
                if len(reservoir) < K:
                    reservoir.append(item)
                else:
                    j = random.randint(0, count - 1)
                    if j < K:
                        reservoir[j] = item

    print(f"scanned lines: {total_lines}, candidate chunks: {count}, kept: {len(reservoir)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(DOCS_PATH, "w", encoding="utf-8") as f:
        for d in reservoir:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"wrote {DOCS_PATH} ({len(reservoir)} docs)")


if __name__ == "__main__":
    main()
