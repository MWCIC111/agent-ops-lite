"""fetch_ivd_corpus.py — 抓取 IVD 域语料源文件（NMPA 器审中心指导原则）

来源：开源法规知识库 `RASAAS/docmcp-knowledge`（CC BY 4.0）的 `nmpa/guidance/*.zh.md`
—— 每篇为文档正文 Markdown，顶部带 YAML frontmatter（`id` / `title.zh` /
`document_number` / `source_url` / `effective_date`），**可逐篇回溯到器审中心原始发布页**。

网络通道（按优先级，本机实测 github.com 与 raw.githubusercontent.com 不可达）：
  1. cdn.jsdelivr.net/gh/<repo>@main/<path>   ← 主通道
  2. gh-proxy.com/https://raw.githubusercontent.com/<repo>/main/<path>
  3. api.github.com/repos/<repo>/contents/<path>（Accept: raw）← 兜底但限流 60/h

两步流程
--------
    # ① 列目录 + 取分类索引（_index.json 自带 ivd 等 20 个类目）
    python scripts/fetch_ivd_corpus.py scan
    # ② 批量下载正文（多线程、断点续传：已存在且 >200B 的文件跳过）
    python scripts/fetch_ivd_corpus.py fetch

    产物：<stage>/raw_md/*.md     逐篇正文
         <stage>/manifest.json   逐篇元数据（file / id / title_zh / doc_number /
                                 source_url / effective_date / category / h2 / h3）
         <stage>/_index.json     仓库分类索引

    再切分成语料（两种策略）：
    python scripts/build_ivd_corpus.py --src <stage>/raw_md --manifest <stage>/manifest.json \
        --strategy section --categories ivd

用法要点
--------
- `--stage`（默认 `rag_data/_src`，已 gitignore）只放**中间产物**；最终语料由
  `build_ivd_corpus.py` 写到 `rag_data/`。
- `--limit N` 只下前 N 篇，用于验证通道连通性。
- 全量约 830 篇 / 26 MB，8 线程约 5~8 分钟。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

UPSTREAM = "RASAAS/docmcp-knowledge"
BRANCH = "main"
GUIDANCE_DIR = "nmpa/guidance"
UA = "Mozilla/5.0 (compatible; agent-ops-lite corpus-builder)"


# ------------------------------------------------------------------ 网络
def _read(url: str, accept: str | None = None, timeout: int = 45, tries: int = 3) -> bytes:
    hdr = {"User-Agent": UA}
    if accept:
        hdr["Accept"] = accept
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def fetch(raw_path: str, timeout: int = 45):
    """按通道优先级取单文件字节，返回 (bytes, 通道名)。"""
    enc = urllib.parse.quote(raw_path, safe="/")
    last = None
    for u in (
        f"https://cdn.jsdelivr.net/gh/{UPSTREAM}@{BRANCH}/{enc}",
        f"https://gh-proxy.com/https://raw.githubusercontent.com/{UPSTREAM}/{BRANCH}/{enc}",
    ):
        try:
            b = _read(u, timeout=timeout, tries=1)
            if b:
                return b, u.split("/")[2]
        except Exception as e:  # noqa: BLE001
            last = e
    try:
        b = _read(f"https://api.github.com/repos/{UPSTREAM}/contents/{enc}",
                  accept="application/vnd.github.raw", timeout=timeout, tries=2)
        return b, "api.github.com"
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"三通道均失败（最后: {last} / api: {e}）")


def list_dir(path: str):
    d = json.loads(_read(f"https://api.github.com/repos/{UPSTREAM}/contents/{path}",
                         accept="application/vnd.github+json").decode("utf-8"))
    return [(it["type"], it["path"], it.get("size", 0)) for it in d]


# ------------------------------------------------------------------ frontmatter
def parse_frontmatter(text: str):
    """极简 frontmatter 解析：flat 标量 + title.zh / title.en。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_raw, body = text[3:end], text[end + 4:].lstrip("\n")
    meta, in_title = {}, False
    for line in fm_raw.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(\s*)([\w\-]+):\s?(.*)$", line)
        if not m:
            continue
        indent, key, val = m.group(1), m.group(2), m.group(3).strip()
        if indent:
            if in_title and key in ("zh", "en") and val:
                meta[f"title_{key}"] = val.strip("'\"")
            continue
        in_title = key == "title"
        if key == "title":
            continue
        meta[key] = val.strip("'\"")
    return meta, body


# ------------------------------------------------------------------ 两种模式
def cmd_scan(stage: str) -> None:
    idx = os.path.join(stage, "_index.json")
    if not os.path.exists(idx):
        b, chan = fetch(f"{GUIDANCE_DIR}/_index.json")
        with open(idx, "wb") as f:
            f.write(b)
        print(f"✔ _index.json（{len(b)} B，via {chan}）")
    else:
        print(f"· _index.json 已存在（{os.path.getsize(idx)} B）")

    entries = list_dir(GUIDANCE_DIR)
    blobs = [(p, s) for t, p, s in entries if t == "file" and p.endswith(".zh.md")]
    out = os.path.join(stage, "_guidance_files.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{"path": p, "size": s} for p, s in blobs], f, ensure_ascii=False, indent=1)
    tot = sum(s for _, s in blobs)
    print(f"✔ 中文正文 {len(blobs)} 篇 / {tot / 1024 / 1024:.1f} MB → {out}")
    print("  下一步：python scripts/fetch_ivd_corpus.py fetch")


def cmd_fetch(stage: str, limit: int, workers: int) -> None:
    files_json = os.path.join(stage, "_guidance_files.json")
    if not os.path.exists(files_json):
        print("✗ 缺 _guidance_files.json，先跑：python scripts/fetch_ivd_corpus.py scan")
        sys.exit(1)
    raw = os.path.join(stage, "raw_md")
    os.makedirs(raw, exist_ok=True)

    items = json.load(open(files_json, encoding="utf-8"))
    if limit:
        items = items[:limit]
    print(f"待抓取 {len(items)} 篇 → {raw}", flush=True)

    lock = threading.Lock()
    done = [0]

    def one(item):
        path = item["path"]
        stem = path.rsplit("/", 1)[-1][: -len(".zh.md")]
        out = os.path.join(raw, stem + ".md")
        if os.path.exists(out) and os.path.getsize(out) > 200:
            with lock:
                done[0] += 1
            return {"file": stem, "path": path, "size": os.path.getsize(out), "cached": True}
        try:
            b, chan = fetch(path)
        except Exception as e:  # noqa: BLE001
            return {"file": stem, "path": path, "error": str(e)[:200]}
        with open(out, "wb") as f:
            f.write(b)
        meta, body = parse_frontmatter(b.decode("utf-8", "replace"))
        rec = {
            "file": stem, "path": path, "size": len(b), "channel": chan,
            "id": meta.get("id", ""), "title_zh": meta.get("title_zh", ""),
            "doc_number": meta.get("document_number", ""),
            "source_url": meta.get("source_url", ""),
            "effective_date": meta.get("effective_date", ""),
            "body_chars": len(body),
            "h2": len(re.findall(r"^##\s", body, re.M)),
            "h3": len(re.findall(r"^###\s", body, re.M)),
        }
        with lock:
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"  ... {done[0]}/{len(items)}", flush=True)
        return rec

    t0 = time.time()
    recs, errs = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(one, it) for it in items]):
            r = f.result()
            (errs if "error" in r else recs).append(r)

    # 交叉 _index.json 补类目（ivd / lab_equipment / blood_transfusion …）
    idx = json.load(open(os.path.join(stage, "_index.json"), encoding="utf-8"))
    entries = idx["entries"] if isinstance(idx, dict) and "entries" in idx else idx
    by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}
    by_slug = {e.get("slug"): e for e in entries if isinstance(e, dict) and e.get("slug")}
    for r in recs:
        e = by_id.get(r.get("id")) or by_slug.get(r["file"]) or {}
        r["category"] = e.get("category", "")
        r["title_zh"] = r.get("title_zh") or (e.get("title", {}) or {}).get("zh", "")
        r["doc_number"] = r.get("doc_number") or e.get("doc_number", "")
        r["classification_codes"] = e.get("classification_codes", [])

    with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)
    with open(os.path.join(stage, "_errors.json"), "w", encoding="utf-8") as f:
        json.dump(errs, f, ensure_ascii=False, indent=1)

    import collections
    cat = collections.Counter(r.get("category") or "(未匹配)" for r in recs)
    print(f"\n完成：成功 {len(recs)}，失败 {len(errs)}，耗时 {time.time() - t0:.0f}s，"
          f"总字节 {sum(r['size'] for r in recs) / 1024 / 1024:.1f} MB")
    print("=== 类目分布 ===")
    for k, v in cat.most_common():
        print(f"  {v:>4}  {k}")
    print("\n下一步：\n  python scripts/build_ivd_corpus.py "
          f"--src {raw} --manifest {os.path.join(stage, 'manifest.json')} "
          "--strategy section --categories ivd")


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取 IVD 域语料源文件（NMPA 指导原则）")
    ap.add_argument("mode", choices=["scan", "fetch"])
    ap.add_argument("--stage", default=os.path.join(_REPO_ROOT, "rag_data", "_src"),
                    help="中间产物目录（默认 rag_data/_src，已 gitignore）")
    ap.add_argument("--limit", type=int, default=0, help="fetch 模式：只下前 N 篇（连通性验证）")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数")
    args = ap.parse_args()

    os.makedirs(args.stage, exist_ok=True)
    if args.mode == "scan":
        cmd_scan(args.stage)
    else:
        cmd_fetch(args.stage, args.limit, args.workers)


if __name__ == "__main__":
    main()
