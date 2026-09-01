"""11_人工审核回写.py — 研发管家「高容错闭环」演示页

展示低置信回答转入的审核队列；人工审核通过 → 写回检索库（reviewed.jsonl），
下次同类问题即可被 BM25 命中，体现"越用越准"的置信度治理闭环。
"""
from __future__ import annotations

import os
import sys

import streamlit as st
from common import show_clock, page_visit
from op_log import log_operation

# ---- 路径 ----
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
for _p in (_REPO_ROOT, _APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from butler_review import pending, approve  # noqa: E402

st.set_page_config(page_title="人工审核回写 · agent-ops-lite", layout="wide")
st.title("人工审核回写（研发管家高容错闭环）")
show_clock()
page_visit("人工审核回写")
st.caption("低置信回答不硬编，落入此队列；审核通过即写回检索库，下次同类问题可被召回 → 越用越准")

pending_list = pending()
if not pending_list:
    st.success("✅ 当前没有待审核项。低置信问题运行后会自动出现在这里。")
else:
    st.warning(f"当前 {len(pending_list)} 条待审核")
    for item in pending_list:
        rid = item["id"]
        with st.expander(f"▸ {item['query'][:40]}（置信度 {item.get('confidence', 0):.2f}）", expanded=True):
            st.write("**原始问题：**", item["query"])
            st.write("**模型草稿：**", item.get("draft") or "（无）")
            ans = st.text_area("审核补充答案（通过后写回检索库）",
                               value=item.get("draft") or "", key=f"ans_{rid}", height=120)
            if st.button("✅ 审核通过并回写", key=f"app_{rid}"):
                if not ans.strip():
                    st.error("请填写答案后再通过")
                else:
                    ok = approve(rid, ans.strip())
                    if ok:
                        st.success("已回写检索库，该问题下次可直接命中。")
                        log_operation("人工审核回写", "审核通过", f"{item['query'][:40]}")
                        st.rerun()
                    else:
                        st.error("回写失败：记录不存在。")

st.divider()
st.caption("回写文件：rag_data/reviewed.jsonl ｜ 审核记录：agent_ops.db · butler_reviews 表")
