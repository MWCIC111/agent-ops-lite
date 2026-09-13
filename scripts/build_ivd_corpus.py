"""构建 IVD 域 RAG 语料（体外诊断试剂 / NMPA 器审中心指导原则）。

来源
----
`RASAAS/docmcp-knowledge`  →  `nmpa/guidance/<id>.zh.md`
内容许可 CC BY 4.0；每篇正文顶部带 YAML frontmatter，含 `source_url` 与
`document_number`，可逐篇回溯到器审中心原始发布页。

范围
----
默认只取 `category == "ivd"`（体外诊断试剂，173 篇）。仓库自身分好 20 个类目，
其余类目可用 `--categories` 追加（如 `lab_equipment,blood_transfusion`）。

两种切片策略（消融对照用）
--------------------------
- `naive`   朴素标点切分：与 `scripts/build_rag.py::chunk_text` 同一套逻辑
            （按 `[。；;！!？?\\n]` 切、聚合到 350 字、无 overlap、无标题前缀）。
            这是**基线**，等价于「不针对文档结构做任何优化」。
- `section` section-aware 切片：以 `##` / `###` 的标题层级为切分边界，
            chunk 正文前拼接「文档标题 · 一级章节 · 二级章节」上下文前缀。
            长章节再按段落聚合到 MAX_CHARS；单段超长时回退到句子切分。

输出
----
`rag_data/docs_ivd_<strategy>.jsonl`  每行 {id, title, content, source, doc_id, section}
`rag_data/ivd_manifest.json`          逐篇元数据（标题 / 类目 / 文号 / 来源 URL / chunk 数）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_OUT_DIR = os.path.join(_REPO_ROOT, "rag_data")

# 朴素基线的参数：与 scripts/build_rag.py 保持一致
NAIVE_SPLIT_RE = re.compile(r"[。；;！!？?\n]")
NAIVE_CHUNK_SIZE = 350

# section-aware 的参数
SEC_MAX_CHARS = 500     # 单个 chunk 目标上限（含前缀）
MIN_CHUNK_CHARS = 40    # 丢弃过短碎片，避免噪声 chunk 干扰 BM25 统计

H1_RE = re.compile(r"^#\s+(.*\S)\s*$")
H2_RE = re.compile(r"^##\s+(?!#)(.*\S)\s*$")
H3_RE = re.compile(r"^###\s+(.*\S)\s*$")
_MD_HEAD_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)  # 剥标记、留文字（naive 基线用）
MD_NOISE_RE = re.compile(r"^\s*(\*\*\*|\*\*|\*|---|===\s*$)")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


# ---------------------------------------------------------------- frontmatter
def parse_frontmatter(text: str):
    """极简 frontmatter 解析：flat 标量 + title.zh / title.en。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_raw, body = text[3:end], text[end + 4 :].lstrip("\n")
    meta, in_title = {}, False
    for line in fm_raw.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(\s*)([\w\-]+):\s?(.*)$", line)
        if not m:
            continue
        indent, key, val = m.group(1), m.group(2), m.group(3).strip().strip("'\"")
        if indent:
            if in_title and key in ("zh", "en"):
                meta[f"title_{key}"] = val
            continue
        in_title = key == "title"
        if key != "title":
            meta[key] = val
    return meta, body


def clean_body(body: str) -> str:
    """去掉 markdown 装饰噪声，保留章节结构与正文（BM25 只吃纯文本）。"""
    lines = []
    for ln in body.split("\n"):
        if MD_NOISE_RE.match(ln):
            continue
        ln = IMG_RE.sub("", ln)
        ln = LINK_RE.sub(r"\1", ln)
        ln = ln.replace("****", "").replace("**", "")
        lines.append(ln.rstrip())
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- 切片策略
def chunk_naive(body: str):
    """朴素标点切分 —— 与 scripts/build_rag.py::chunk_text 同一套逻辑。

    公平性说明：先把 markdown 标题标记（`#`/`##`/`###`）剥掉、**但保留标题文字**。
    否则基线语料里会残留 `###` 这类标记 token，使「朴素 vs section-aware」的差异
    部分来自标记噪声而非切片策略，消融就失去意义。剥标记后的基线 = 「同样一份纯文本，
    不做章节边界与上下文前缀」——这才是 section-aware 应当对比的东西。
    """
    text = _MD_HEAD_RE.sub(r"\1", body).strip()
    if len(text) <= NAIVE_CHUNK_SIZE:
        return [text] if len(text) >= MIN_CHUNK_CHARS else []
    parts = [p for p in NAIVE_SPLIT_RE.split(text) if p.strip()]
    chunks, buf = [], ""
    for p in parts:
        if len(buf) + len(p) < NAIVE_CHUNK_SIZE:
            buf += p + "。"
        else:
            if buf:
                chunks.append(buf.strip())
            buf = p + "。"
    if buf:
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) >= 20]


def _segments(body: str):
    """按 ## / ### 把正文切成 (heading_path, text)。# 视为文档标题，不入 path。"""
    segs, path, buf = [], [], []

    def flush():
        txt = "\n".join(buf).strip()
        if txt:
            segs.append((list(path), txt))

    for ln in body.split("\n"):
        m2, m3, m1 = H2_RE.match(ln), H3_RE.match(ln), H1_RE.match(ln)
        if m2:
            flush()
            buf = []
            path = [m2.group(1).strip()]
        elif m3:
            flush()
            buf = []
            path = (path[:1] or []) + [m3.group(1).strip()]
        elif m1:
            flush()
            buf = []
            path = []
        else:
            buf.append(ln)
    flush()
    return segs


def _pack_by_paragraph(text: str, limit: int):
    """按空行分段，贪心聚合到 limit 字；单段超长时回退句子切分。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in paras:
        if len(p) > limit:
            if buf:
                out.append(buf.strip())
                buf = ""
            for sub in _split_sentence(p, limit):
                out.append(sub)
            continue
        if len(buf) + len(p) + 1 <= limit:
            buf = f"{buf}\n{p}" if buf else p
        else:
            out.append(buf.strip())
            buf = p
    if buf:
        out.append(buf.strip())
    return out


def _split_sentence(text: str, limit: int):
    """单段超长：按标点切句再聚合（这是 section-aware 内部的兜底，非基线策略）。"""
    parts = [s for s in NAIVE_SPLIT_RE.split(text) if s.strip()]
    out, buf = [], ""
    for p in parts:
        if len(buf) + len(p) + 1 <= limit:
            buf = f"{buf}{p}。" if buf else p + "。"
        else:
            if buf:
                out.append(buf.strip())
            buf = p + "。"
    if buf:
        out.append(buf.strip())
    return out


def chunk_section(body: str, doc_title: str):
    """section-aware 切片：以标题层级为界，chunk 前缀带章节路径上下文。"""
    chunks = []
    for path, text in _segments(body):
        prefix = " · ".join([doc_title] + [p for p in path if p])
        budget = max(120, SEC_MAX_CHARS - len(prefix) - 3)
        pieces = [text] if len(text) <= budget else _pack_by_paragraph(text, budget)
        for i, piece in enumerate(pieces):
            c = piece.strip()
            if len(c) < MIN_CHUNK_CHARS:
                continue
            # 多段拆分时，后续片段标注续接序号，便于人工审核定位
            head = prefix if len(pieces) == 1 else f"{prefix}（{i + 1}/{len(pieces)}）"
            chunks.append({"content": f"{head}：{c}", "section": " · ".join(path)})
    if not chunks:  # 无章节结构时退化为整篇
        chunks = [{"content": f"{doc_title}：{body}", "section": ""}]
    return chunks


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                    help="raw_md 目录（*.md，含 frontmatter）")
    ap.add_argument("--manifest", default=None,
                    help="build_corpus.py 产出的 manifest.json；"
                         "类目（ivd 等）只存在于仓库 _index.json，必须由它提供")
    ap.add_argument("--strategy", choices=["naive", "section"], default="section")
    ap.add_argument("--categories", default="ivd",
                    help="逗号分隔的类目白名单；空字符串=全部类目")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-docs", type=int, default=0, help="0=不限（调试用）")
    args = ap.parse_args()

    cats = {c.strip() for c in args.categories.split(",") if c.strip()}
    out_path = args.out or os.path.join(_OUT_DIR, f"docs_ivd_{args.strategy}.jsonl")

    # 元数据优先取 manifest（含 ivd/lab_equipment 等类目），回退到 frontmatter
    by_stem = {}
    if args.manifest:
        for r in json.load(open(args.manifest, encoding="utf-8")):
            by_stem[r.get("file", "")] = r

    files = sorted(f for f in os.listdir(args.src) if f.endswith(".md"))
    docs, chunks, skipped = [], [], []
    cid = 0
    for fn in files:
        p = os.path.join(args.src, fn)
        try:
            raw = open(p, encoding="utf-8").read()
        except OSError:
            continue
        meta, body = parse_frontmatter(raw)
        man = by_stem.get(fn[:-3], {})
        cat = man.get("category") or meta.get("category", "")
        if cats and cat not in cats:
            continue
        title = (man.get("title_zh") or meta.get("title_zh")
                 or meta.get("id") or fn[:-3])
        doc_number = man.get("doc_number") or meta.get("document_number", "")
        source_url = man.get("source_url") or meta.get("source_url", "")
        effective_date = man.get("effective_date") or meta.get("effective_date", "")
        body = clean_body(body)
        if not body:
            continue
        doc_id = man.get("id") or meta.get("id") or fn[:-3]
        if args.strategy == "naive":
            pieces = [{"content": c, "section": ""} for c in chunk_naive(body)]
        else:
            pieces = chunk_section(body, title)
        if not pieces:
            skipped.append(fn)
            continue
        docs.append({
            "doc_id": doc_id,
            "title": title,
            "category": cat,
            "doc_number": doc_number,
            "source_url": source_url,
            "effective_date": effective_date,
            "file": fn,
            "n_chunks": len(pieces),
            "chars": len(body),
        })
        for pc in pieces:
            chunks.append({
                "id": cid,
                "title": title,
                "content": pc["content"],
                "source": f"NMPA 器审中心指导原则（{doc_number or doc_id}）",
                "doc_id": doc_id,
                "section": pc["section"],
            })
            cid += 1
        if args.max_docs and len(docs) >= args.max_docs:
            break

    os.makedirs(_OUT_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    json.dump(docs, open(os.path.join(_OUT_DIR, "ivd_manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    lens = sorted(len(c["content"]) for c in chunks)
    print(f"策略={args.strategy}  类目={sorted(cats) or 'ALL'}")
    print(f"文档 {len(docs)} 篇 → chunk {len(chunks)} 个  跳过 {len(skipped)} 篇")
    if lens:
        print(f"chunk 长度: min={lens[0]} p50={lens[len(lens)//2]} "
              f"p90={lens[int(len(lens)*0.9)]} max={lens[-1]} "
              f"均值={sum(lens)/len(lens):.0f}")
    print(f"总字符 {sum(lens)/10000:.1f} 万字")
    print(f"→ {out_path}")


if __name__ == "__main__":
    sys.exit(main())
