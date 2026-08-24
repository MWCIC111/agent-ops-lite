"""alerts.py — 告警 Webhook

消费 report() 聚合指标，错误率 / 成本 / 延迟超过阈值时，
向企业微信 / 飞书机器人推送消息。

零依赖：仅用标准库 urllib 发送 POST，不引入 requests。
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any


def _build_payload(webhook_type: str, content: str) -> dict:
    """按平台构造机器人消息体：wecom（企业微信）/ feishu（飞书）"""
    if webhook_type == "feishu":
        return {"msg_type": "text", "content": {"text": content}}
    return {"msgtype": "text", "text": {"content": content}}  # wecom 默认


@dataclass
class AlertRule:
    """一条阈值规则。

    metric 对应 report()['total'] 里的指标键：
      - error_rate        错误率（0~1）
      - total_cost_usd    总成本（美元）
      - avg_latency_ms    平均延迟（毫秒）
    """

    metric: str
    op: str  # ">" 或 ">="
    threshold: float
    label: str = ""  # 规则名称，用于消息可读性

    def hit(self, value: float) -> bool:
        if self.op == ">=":
            return value >= self.threshold
        return value > self.threshold


@dataclass
class AlertEvent:
    """一次触发的告警事件"""

    rule: AlertRule
    value: float
    window: str = ""
    at: float = field(default_factory=time.time)

    def render(self) -> str:
        """渲染为人类可读的告警文本"""
        return (
            "⚠️ agent-ops-lite 告警\n"
            f"规则：{self.rule.label or self.rule.metric}\n"
            f"当前值：{self.value:.4f}（阈值 {self.rule.op} {self.rule.threshold}）\n"
            f"窗口：{self.window or '—'}"
        )


DEFAULT_RULES = [
    AlertRule("error_rate", ">", 0.10, "错误率超 10%"),
    AlertRule("total_cost_usd", ">", 1.0, "总成本超 $1"),
]


class WebhookAlert:
    """把聚合指标与阈值规则比对，超限时向机器人 Webhook 推送。

    用法：
        alert = WebhookAlert(webhook_url=YOUR_WECHAT_ROBOT_URL)
        fired = alert.check_and_send(report())   # 一条调用：检查 + 发送
    """

    def __init__(
        self,
        webhook_url: str,
        rules: list[AlertRule] | None = None,
        webhook_type: str = "wecom",
        timeout: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type
        self.timeout = timeout
        self.enabled = enabled
        self.rules = rules if rules is not None else list(DEFAULT_RULES)

    def check(self, rep: dict[str, Any]) -> list[AlertEvent]:
        """比对 report 输出，返回所有触发的事件（不发送）"""
        total = rep.get("total", {})
        events = []
        for rule in self.rules:
            value = total.get(rule.metric)
            if value is not None and rule.hit(value):
                events.append(AlertEvent(rule=rule, value=value, window=rep.get("window", "")))
        return events

    def send(self, event: AlertEvent) -> bool:
        """发送单条告警。成功返回 True；被禁用 / 网络失败返回 False。"""
        if not self.enabled:
            return False
        payload = _build_payload(self.webhook_type, event.render())
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def check_and_send(self, rep: dict[str, Any]) -> list[AlertEvent]:
        """检查并发送所有触发的告警，返回实际发送成功的事件列表"""
        sent = []
        for ev in self.check(rep):
            if self.send(ev):
                sent.append(ev)
        return sent


def send_alert(
    webhook_url: str,
    content: str,
    webhook_type: str = "wecom",
    timeout: float = 5.0,
) -> bool:
    """便捷函数：不建规则，直接发送一条文本告警。"""
    payload = _build_payload(webhook_type, content)
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False
