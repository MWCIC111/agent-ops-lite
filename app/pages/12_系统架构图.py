# 12_系统架构图.py —— 研发管家 · 系统架构全景（archify 可视化）
# 用 archify 编译出的自包含 HTML 嵌入展示，作为「7_Agent拓扑」的补充：
#   页 7 证真（代码实时画 Plotly 拓扑、异常可联动告警页），
#   本页讲全（端到端：编排→工具→置信度→门控→人审→回写→Trace→交付）。
# 面试叙事："左边看真实拓扑联动，这页看完整架构故事线——置信度门控怎么放、怎么交付。"
# 注意：archify 产物默认 data-preset="classic"（实线可见），可现场切 SIGNAL FLOW 看动态 trace，截图请回 CLASSIC。

import os

import streamlit as st
from streamlit.components.v1 import html as st_html

from common import show_clock, page_visit

st.set_page_config(page_title="系统架构全景 · 研发管家", page_icon="🏛️", layout="wide")

st.title("🏛️ 系统架构全景 · 研发管家")
show_clock()
page_visit("系统架构图")
st.caption("Orchestrator 集中式编排 + 置信度门控 + 全链路 Trace + 人工审核回写 + 最终交付。"
           "右上角预设默认为 CLASSIC（实线可见）；可现场切 SIGNAL FLOW 看动态 trace，但截图请回 CLASSIC。")

# 多候选路径：避免依赖 __file__ 单点解析，兼容本地开发与服务器 systemd 模式。
CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "research-butler-architecture.html"),
    os.path.join(os.getcwd(), "assets", "research-butler-architecture.html"),
    os.path.join(os.getcwd(), "app", "assets", "research-butler-architecture.html"),
    "/home/ubuntu/agent-ops-lite/app/assets/research-butler-architecture.html",
]
HTML_PATH = next((p for p in CANDIDATES if os.path.exists(p)), None)

if HTML_PATH is None:
    st.error(
        "未找到架构图资产。请确认 app/assets/research-butler-architecture.html 已随仓库部署。\n\n"
        f"已尝试路径：\n- " + "\n- ".join(CANDIDATES)
    )
    st.stop()

with open(HTML_PATH, encoding="utf-8") as f:
    archify_html = f.read()

st_html(archify_html, height=860, scrolling=True)

with open(HTML_PATH, "rb") as f:
    st.download_button(
        "⬇️ 下载架构图 HTML（自包含，可离线双击打开）",
        data=f.read(),
        file_name="research-butler-architecture.html",
        mime="text/html",
    )
