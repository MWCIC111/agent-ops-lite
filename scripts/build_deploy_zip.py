"""build_deploy_zip.py — 构建 agent-ops-lite 部署包（本地侧，无需联网）

用法：
    python scripts/build_deploy_zip.py [输出路径]

排除项（与部署 skill 一致）：
    .git / __pycache__ / *.pyc / .workbuddy / *.db / .env /
    rag_data/*.pkl / *.bak
（.env 含 API Key 不进包；agent_ops.db / reviewed.jsonl 为运行时数据，服务器侧保留）

注意：服务器无法从 GitHub 拉取，部署走"本地 zip → 腾讯云控制台上传 →
OrcaTerm 解压 → systemctl 重启"。本脚本只负责产出 zip。
"""
from __future__ import annotations

import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDE_DIRS = {".git", "__pycache__", ".workbuddy", ".idea", ".vscode"}
EXCLUDE_EXTS = {".pyc", ".db", ".pkl", ".bak"}
EXCLUDE_NAMES = {".env", ".env.example"}


def _should_exclude(root: str, name: str) -> bool:
    if name in EXCLUDE_DIRS:
        return True
    if name in EXCLUDE_NAMES:
        return True
    _, ext = os.path.splitext(name)
    if ext.lower() in EXCLUDE_EXTS:
        return True
    # rag_data 下的 .pkl 缓存（运行时重建）
    if name.endswith(".pkl") and "rag_data" in root.replace("\\", "/"):
        return True
    return False


def build(out_path: str) -> int:
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            # 原地剪枝，避免深入 excluded 目录
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if _should_exclude(dirpath, fn):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, REPO_ROOT)
                zf.write(full, rel)
                count += 1
    return count


if __name__ == "__main__":
    default_out = os.path.join(os.path.dirname(REPO_ROOT), "agent-ops-lite-deploy.zip")
    out = sys.argv[1] if len(sys.argv) > 1 else default_out
    n = build(out)
    print(f"打包完成：{out}（{n} 个文件）")
