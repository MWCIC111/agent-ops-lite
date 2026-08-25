---
name: agentops-observe
description: 给任意 Agent 加可观测性 —— 3 行接入采集、指标聚合、成本核算、告警推送与持久化存储。当用户想让自己的 Agent/工作流获得调用日志、延迟/成本指标、错误定位或告警能力时使用本 skill。
version: 1.0.0
license: MIT
---

# agentops-observe

给任意 Agent 加可观测性：**3 行接入**，拿到全链路 Trace、指标、成本与告警。核心能力由零依赖库 `agent_ops` 提供，本 skill 负责教会 Agent 怎么用。

## 何时使用

- 用户想让自己的 Agent / 工作流有调用日志（Trace）和指标
- 用户想按模型 / 按 Agent 核算 token 成本
- 用户想在错误率 / 成本超阈值时收到企业微信或飞书告警
- 用户想让采集数据持久化（SQLite），重启不丢

## 三步接入（最小闭环）

```python
from agent_ops import trace, record_step, report

@trace(agent="检索 Agent", model="qwen-max")   # 1. 装饰器包住入口函数
def my_agent(question: str) -> str:
    record_step("知识检索", model="qwen-plus", tool="知识库检索",
                tokens_in=500, tokens_out=200)  # 2. 函数内每步一行 record_step
    return "答案"

my_agent("什么是 AgentOps?")
print(report())                                # 3. 一键出报告
```

## 核心 API 速查

| API | 作用 |
|---|---|
| `@trace(agent=..., model=...)` | 装饰器：自动生成 trace_id / 计时；函数抛异常自动标记 `failed` 并记录错误 |
| `record_step(name, model=, tool=, tokens_in=, tokens_out=, latency_ms=)` | 记录一步；`latency_ms` 缺省时自动计时 |
| `with span("名称", model=...)` | 父子 span：块内步骤自动挂成 children，聚合时递归归因 |
| `report(since=)` | 聚合：`{total, by_agent, by_model, window}` |
| `model_usage()` | 按模型归因调用数 / token / 成本，成本降序 |
| `AlertRule(metric, op, threshold, label)` | 阈值规则，`metric` 支持 `error_rate` / `total_cost_usd` / `avg_latency_ms` |
| `WebhookAlert(webhook_url=, webhook_type=, rules=)` | 告警推送，`webhook_type` 支持 `wecom`（企业微信）/ `feishu`（飞书） |
| `SQLiteStore(path)` / `Collector(storage=...)` | 持久化：落库、重启恢复、trace_id 幂等 |

## 典型用法

### 1. 只看指标报告

```bash
python scripts/observe_agent.py --demo
```

演示完整闭环：采集 → 报告 → 告警规则命中 → SQLite 落库 → 模拟重启恢复。

### 2. 采集自己的 Agent

```python
from agent_ops import trace, record_step

@trace(agent="我的 Agent", model="qwen-plus")
def my_agent(q: str) -> str:
    record_step("意图识别", model="qwen-plus", tokens_in=120, tokens_out=40)
    record_step("工具调用", tool="查询API", tokens_in=300, tokens_out=80, latency_ms=450)
    return "done"
```

### 3. 告警推送（企业微信 / 飞书）

```python
from agent_ops import AlertRule, WebhookAlert, report

rules = [
    AlertRule("error_rate", ">", 0.10, "错误率超 10%"),
    AlertRule("total_cost_usd", ">=", 1.0, "成本超 $1"),
]
alert = WebhookAlert(webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
                     webhook_type="wecom", rules=rules)
alert.check_and_send(report())   # 一条调用完成「检查 + 发送」
```

### 4. SQLite 持久化（重启不丢）

```python
from agent_ops import Collector, SQLiteStore

col = Collector(storage=SQLiteStore("ops.db"))   # add() 自动双写内存 + 落库
# ... 采集 ...
fresh = Collector(storage=SQLiteStore("ops.db")) # 模拟重启
print(fresh.traces())                            # 历史自动恢复
```

## 注意事项

- `agent_ops` 是**零依赖**纯标准库实现，无需安装第三方包，把 `agent_ops/` 目录放进项目即可
- 未知模型会抛 `KeyError` —— 在 `agent_ops/cost.py` 的 `MODEL_PRICE` 补单价即可，避免静默算错账
- 采集的数据结构与 `app/demo_data.py` 完全一致，面板可直接消费
- 换存储后端（Elasticsearch / ClickHouse）：实现 `TraceStore` 协议（`save/load/clear`）即可，上层零改动
