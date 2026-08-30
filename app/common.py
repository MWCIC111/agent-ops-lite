"""app/common.py — 公共 UI 组件（实时时钟 + 页面访问记录）

供所有页面复用，避免重复内联 JS / 重复 import。
"""
from __future__ import annotations

import streamlit.components.v1 as components
from op_log import log_operation


def show_clock():
    """在页面顶部显示访问者本地实时时钟（秒级跳动，纯前端、不耗服务器资源）。

    取的是访问者浏览器本地时间，即「操作人员当下操作的现实时间」。
    """
    components.html(
        """
        <div id="liveclock"
             style="font-size:13px;color:#8a8a8a;font-family:ui-monospace,Menlo,Consolas,monospace;"></div>
        <script>
        (function () {
          function pad(n) { return String(n).padStart(2, '0'); }
          function tick() {
            var d = new Date();
            var s = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
                    pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
            var el = document.getElementById('liveclock');
            if (el) el.textContent = '🕐 当前现实时间（本地）：' + s;
          }
          tick();
          setInterval(tick, 1000);
        })();
        </script>
        """,
        height=30,
    )


def page_visit(page: str):
    """记录一次页面访问（带 5 秒同页节流，避免 Streamlit rerun 刷屏）。"""
    log_operation(page, "访问", throttle_key=page, throttle_sec=5)
