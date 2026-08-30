"""10_操作日志.py — 操作人员操作时间线（时间追踪 / 审计）

汇总各页面记录的操作真实时间戳：运行真实 Agent、播种、清空数据、访问页面等，
满足「操作人员在什么时间做了什么」的追踪需求。数据来自 operations.log（JSONL）。
"""
from __future__ import annotations

import os
import sys

import streamlit as st

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common import show_clock, page_visit  # noqa: E402
from op_log import load_operations  # noqa: E402

st.set_page_config(page_title="操作日志 · agent-ops-lite", layout="wide")
st.title("🕓 操作日志（时间追踪）")
show_clock()
page_visit("操作日志")

st.caption("记录操作人员在各页面的真实操作时间（取访问者本地时间）。"
           "每次运行真实 Agent、播种、清空数据，以及访问页面，都会留下时间戳，便于演示与审计追踪。")

ops = load_operations(200)
if not ops:
    st.info("暂无操作记录。访问任意页面或运行一次真实 Agent 后，这里会出现时间线。"
            "（operations.log 位于仓库根目录）")
else:
    st.metric("累计操作次数", len(ops))
    c1, c2 = st.columns(2)
    c1.metric("最早记录", ops[0]["ts"])
    c2.metric("最近记录", ops[-1]["ts"])

    rows = [
        {
            "时间": o["ts"],
            "页面": o["page"],
            "操作": o["action"],
            "详情": o.get("detail", ""),
        }
        for o in ops
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
