#!/usr/bin/env python3
"""apply_patches.py — 在云服务器上为 agent-ops-lite 接入「真实 LLM Agent（DeepSeek API）」。

用法（在服务器上，venv 已激活、位于仓库根 /home/ubuntu/agent-ops-lite）：
    python apply_patches.py

本脚本只做两处仓库内改动 + 一处依赖，幂等（重复运行安全）：
  1. agent_ops/cost.py  —— 未知模型兜底为 0 价，避免 summarize() 抛 KeyError
  2. app/demo_data.py   —— load_demo_traces() 追加真实落库 Trace（全面板可消费）
  3. app/requirements.txt —— 确保含 openai
页面文件请单独把 real_agent_page.py 拷到 app/pages/8_真实Agent.py（见指南）。

安全注意：API Key 不写在仓库里；通过 /home/ubuntu/agent-ops-lite/.env 注入，由 systemd 加载。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.dirname(__file__))


def patch_cost_py() -> None:
    p = os.path.join(ROOT, "agent_ops", "cost.py")
    with open(p, "r", encoding="utf-8") as f:
        s = f.read()
    old = 'raise KeyError(f"未知模型: {model}，请在 MODEL_PRICE 中补充单价")'
    new = "price = (0.0, 0.0)  # 演示阶段：未知模型按 0 价计，避免模型名变化导致 summarize() 崩溃"
    if old in s:
        s = s.replace(old, new)
        with open(p, "w", encoding="utf-8") as f:
            f.write(s)
        print("[ok] agent_ops/cost.py  未知模型兜底为 0 价")
    elif "未知模型按 0 价计" in s:
        print("[skip] agent_ops/cost.py 已是 0 价兜底")
    else:
        print("[warn] agent_ops/cost.py 未找到目标行，请手动检查")


def patch_demo_data_py() -> None:
    p = os.path.join(ROOT, "app", "demo_data.py")
    with open(p, "r", encoding="utf-8") as f:
        s = f.read()

    # 1) 顶部加 os / sys.path
    if "_REPO_ROOT" not in s:
        s = s.replace(
            "import random\n",
            "import os\nimport random\nimport sys\n\n"
            "_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
            "if _REPO_ROOT not in sys.path:\n"
            "    sys.path.insert(0, _REPO_ROOT)\n",
            1,
        )

    # 2) 追加 load_real_traces()
    if "def load_real_traces" not in s:
        s = s.replace(
            "def load_demo_traces(",
            "def load_real_traces():\n"
            '    """读取真实落库的 Trace（来自真实 LLM API 调用），供全面板消费。"""\n'
            "    try:\n"
            "        from agent_ops.storage import SQLiteStore\n"
            '        real_store = SQLiteStore(os.path.join(_REPO_ROOT, "agent_ops.db"))\n'
            "        return real_store.load()\n"
            "    except Exception:\n"
            "        return []\n\n\n"
            "def load_demo_traces(",
            1,
        )

    # 3) load_demo_traces 末尾追加真实 trace
    tail_old = "    traces.sort(key=lambda t: t.started_at, reverse=True)\n    return traces\n"
    tail_new = (
        "    traces.sort(key=lambda t: t.started_at, reverse=True)\n"
        "    # 追加真实落库的 Trace（来自真实 LLM API 调用），不改动模拟基线\n"
        "    try:\n"
        "        traces.extend(load_real_traces())\n"
        "        traces.sort(key=lambda t: t.started_at, reverse=True)\n"
        "    except Exception:\n"
        "        pass\n"
        "    return traces\n"
    )
    if "load_real_traces()" not in s:
        if tail_old in s:
            s = s.replace(tail_old, tail_new)
        else:
            print("[warn] app/demo_data.py 未找到 load_demo_traces 末尾，请手动合并真实 trace")

    with open(p, "w", encoding="utf-8") as f:
        f.write(s)
    print("[ok] app/demo_data.py  已追加真实 Trace 到全面板")


def patch_requirements() -> None:
    p = os.path.join(ROOT, "app", "requirements.txt")
    with open(p, "r", encoding="utf-8") as f:
        s = f.read()
    if "openai" not in s:
        with open(p, "a", encoding="utf-8") as f:
            f.write("openai\n")
        print("[ok] app/requirements.txt  已追加 openai")
    else:
        print("[skip] app/requirements.txt 已含 openai")


if __name__ == "__main__":
    patch_cost_py()
    patch_demo_data_py()
    patch_requirements()
    print("\n下一步：")
    print("  1. 把 real_agent_page.py 拷到 app/pages/8_真实Agent.py")
    print("  2. 在仓库根创建 .env，写入 DEEPSEEK_API_KEY=sk-...")
    print("  3. 修改 /etc/systemd/system/agent-ops-lite.service，加入 EnvironmentFile=.../.env")
    print("  4. pip install -r app/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
    print("  5. sudo systemctl daemon-reload && sudo systemctl restart agent-ops-lite")
    print("  6. 浏览器打开 http://<你的IP>:8501  → 左侧「真实 Agent」页")
