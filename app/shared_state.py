# shared_state.py —— 全局模拟状态：让 8 个页面共享同一份"线上系统状态"
#
# 设计理念（面试可直接讲）：
#   真实生产里，所有面板读的是同一个后端（Prometheus / ES / Redis）。
#   本 Demo 用 st.session_state 模拟这个"共享后端"，实现跨页面联动：
#     拓扑页标记 Agent 异常 → 告警页立刻亮红灯 → 首页顶部横幅提示
#     版本对比页给出发布结论 → 灰度发布页按结论执行放量 → 异常自动回滚
#   这正是一个真实 AgentOps 系统的交互闭环。
import streamlit as st


def init():
    """初始化全局模拟状态（幂等，任何页面 import 后调用一次即可）"""
    if "sim" not in st.session_state:
        st.session_state.sim = {
            # ---- 7_Agent拓扑.py 写入：哪个 Agent 处于异常 ----
            "abnormal_agent": None,   # 例："推理 Agent"
            # ---- 3_成本核算.py 写入：配额是否熔断 ----
            "quota_breach": False,
            "quota": 80,
            # ---- 5_版本对比.py 写入：发布决策 ----
            "release_decision": None,  # "全量发布" | "灰度验证" | "暂缓发布"
            # ---- 6_灰度发布.py 写入：灰度进度 ----
            "canary_stage": 0,         # 0=10%  1=50%  2=100%
            "canary_abnormal": False,
            "rolled_back": False,
            # ---- Home.py 写入：Live 实时窗口（供其他页面感知最新流量）----
            "live_window": None,
            "live_on": False,
        }


def get():
    """读取全局状态字典"""
    init()
    return st.session_state.sim


def agent_abnormal():
    """当前被标记为异常的 Agent 名（无则返回 None）"""
    return get()["abnormal_agent"]


def release_decision():
    """当前发布决策（全量发布 / 灰度验证 / 暂缓发布 / None）"""
    return get()["release_decision"]
