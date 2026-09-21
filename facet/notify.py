"""通知渠道：Webhook / 企业微信机器人 / Telegram / Server 酱。

设计原则：
  - 通知失败绝不影响采集与告警落库（告警先入库，推送是尽力而为）
  - 支持 dry_run：只打印不发送，便于先验证规则再开启推送
  - 支持静默时段：深夜的捡漏告警未必是你想要的
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

import requests

from .config import NotifyConfig
from .models import AlertEvent

logger = logging.getLogger(__name__)

SEVERITY_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


class Notifier:
    """按配置向多个渠道分发告警。"""

    def __init__(self, config: NotifyConfig) -> None:
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "facet/0.1"})

    # ── 对外入口 ───────────────────────────────────────────

    def send(self, events: Sequence[AlertEvent]) -> list[tuple[AlertEvent, str]]:
        """发送告警，返回 [(事件, 结果描述)]。永不抛异常。"""
        results: list[tuple[AlertEvent, str]] = []
        if not events:
            return results
        for event in events:
            results.append((event, self._send_one(event)))
        return results

    def _send_one(self, event: AlertEvent) -> str:
        if not self.config.enabled:
            return "disabled"
        if self._in_quiet_hours(event.created_at):
            return "quiet_hours_suppressed"
        if self.config.dry_run:
            logger.info("[notify:dry-run] %s", event.message)
            return "dry_run"

        outcomes: list[str] = []
        for channel in self.config.channels:
            kind = str(channel.get("type", "")).lower()
            try:
                if kind == "webhook":
                    outcomes.append(self._post_webhook(channel, event))
                elif kind == "wecom":
                    outcomes.append(self._post_wecom(channel, event))
                elif kind == "telegram":
                    outcomes.append(self._post_telegram(channel, event))
                elif kind == "serverchan":
                    outcomes.append(self._post_serverchan(channel, event))
                else:
                    outcomes.append(f"unknown_type:{kind}")
            except requests.RequestException as exc:
                logger.warning("[notify] %s 渠道发送失败: %s", kind, exc)
                outcomes.append(f"{kind}:network_error")
        return ",".join(outcomes) if outcomes else "no_channels"

    # ── 渠道实现 ───────────────────────────────────────────

    def _post_webhook(self, channel: dict[str, Any], event: AlertEvent) -> str:
        url = channel.get("url")
        if not url:
            return "webhook:missing_url"
        payload = {
            "severity": event.severity,
            "rule": event.rule,
            "market_hash_name": event.market_hash_name,
            "platform": event.platform,
            "current_price": event.current_price,
            "baseline_price": event.baseline_price,
            "change_percent": event.change_percent,
            "message": event.message,
            "created_at": event.created_at.isoformat(),
        }
        resp = self._session.post(url, json=payload, timeout=10)
        return f"webhook:{resp.status_code}"

    def _post_wecom(self, channel: dict[str, Any], event: AlertEvent) -> str:
        """企业微信群机器人。"""
        url = channel.get("url")
        if not url:
            return "wecom:missing_url"
        icon = SEVERITY_ICON.get(event.severity, "")
        content = (f"{icon} **{event.market_hash_name}**\n"
                   f"> 平台：{event.platform}\n"
                   f"> 触发：{event.rule}\n"
                   f"> {event.message}")
        resp = self._session.post(url, json={"msgtype": "markdown",
                                             "markdown": {"content": content}}, timeout=10)
        return f"wecom:{resp.status_code}"

    def _post_telegram(self, channel: dict[str, Any], event: AlertEvent) -> str:
        token = channel.get("bot_token")
        chat_id = channel.get("chat_id")
        if not token or not chat_id:
            return "telegram:missing_config"
        proxy = channel.get("proxy")
        proxies = {"https": proxy, "http": proxy} if proxy else None
        icon = SEVERITY_ICON.get(event.severity, "")
        resp = self._session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{icon} {event.message}"},
            timeout=10, proxies=proxies,
        )
        return f"telegram:{resp.status_code}"

    def _post_serverchan(self, channel: dict[str, Any], event: AlertEvent) -> str:
        send_key = channel.get("send_key")
        if not send_key:
            return "serverchan:missing_key"
        resp = self._session.post(
            f"https://sctapi.ftqq.com/{send_key}.send",
            data={"title": f"[{event.severity}] {event.market_hash_name} {event.rule}",
                  "desp": event.message},
            timeout=10,
        )
        return f"serverchan:{resp.status_code}"

    # ── 静默时段 ───────────────────────────────────────────

    def _in_quiet_hours(self, when: datetime) -> bool:
        qh = self.config.quiet_hours
        if not qh:
            return False
        start, end = qh
        hour = when.astimezone().hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end     # 跨零点，如 23 -> 8

    def close(self) -> None:
        self._session.close()
