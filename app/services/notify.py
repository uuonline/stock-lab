"""消息推送渠道。

支持：企业微信机器人、Telegram、Server酱、Bark、通用 Webhook。
全部通过 .env 配置，留空即自动跳过该渠道。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("stocklab.notify")

TIMEOUT = 15.0


def _post(url: str, **kwargs: Any) -> bool:
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as c:
            r = c.post(url, **kwargs)
            if r.status_code >= 400:
                log.warning("推送失败 %s -> HTTP %s %s", url[:60], r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("推送异常 %s: %s", url[:60], exc)
        return False


def _post_json(url: str, payload: dict) -> bool:
    return _post(url, json=payload)


def send_wecom(content: str, title: str = "") -> bool:
    if not settings.notify_wecom_webhook:
        return False
    body = f"**{title}**\n\n{content}" if title else content
    return _post_json(
        settings.notify_wecom_webhook,
        {"msgtype": "markdown", "markdown": {"content": body}},
    )


def send_telegram(content: str, title: str = "") -> bool:
    if not (settings.notify_telegram_token and settings.notify_telegram_chat):
        return False
    text = f"*{title}*\n\n{content}" if title else content
    return _post_json(
        f"https://api.telegram.org/bot{settings.notify_telegram_token}/sendMessage",
        {"chat_id": settings.notify_telegram_chat, "text": text, "parse_mode": "Markdown"},
    )


def send_serverchan(content: str, title: str = "") -> bool:
    if not settings.notify_serverchan_key:
        return False
    return _post(
        f"https://sctapi.ftqq.com/{settings.notify_serverchan_key}.send",
        data={"title": title or "StockLab 提醒", "desp": content},
    )


def send_bark(content: str, title: str = "") -> bool:
    if not settings.notify_bark_url:
        return False
    base = settings.notify_bark_url.rstrip("/")
    from urllib.parse import quote
    url = f"{base}/{quote(title or 'StockLab')}/{quote(content)}"
    return _post(url, params={"group": "StockLab", "isArchive": 1})


def send_generic(content: str, title: str = "") -> bool:
    if not settings.notify_generic_webhook:
        return False
    return _post_json(
        settings.notify_generic_webhook,
        {"title": title or "StockLab", "content": content, "source": "stocklab"},
    )


CHANNELS = {
    "wecom": (send_wecom, "企业微信机器人"),
    "telegram": (send_telegram, "Telegram"),
    "serverchan": (send_serverchan, "Server酱"),
    "bark": (send_bark, "Bark"),
    "webhook": (send_generic, "通用 Webhook"),
}


def configured_channels() -> list[str]:
    return [k for k, _ in CHANNELS.items() if _is_configured(k)]


def _is_configured(key: str) -> bool:
    return {
        "wecom": bool(settings.notify_wecom_webhook),
        "telegram": bool(settings.notify_telegram_token and settings.notify_telegram_chat),
        "serverchan": bool(settings.notify_serverchan_key),
        "bark": bool(settings.notify_bark_url),
        "webhook": bool(settings.notify_generic_webhook),
    }.get(key, False)


def send(content: str, title: str = "") -> dict[str, bool]:
    """向所有已配置渠道推送，返回各渠道结果。"""
    results: dict[str, bool] = {}
    for key, (fn, _label) in CHANNELS.items():
        if not _is_configured(key):
            continue
        try:
            results[key] = bool(fn(content, title))
        except Exception as exc:  # noqa: BLE001
            log.warning("渠道 %s 推送异常: %s", key, exc)
            results[key] = False
    if not results:
        log.info("未配置任何推送渠道，消息仅记录到日志：%s %s", title, content[:200])
    return results


def status() -> list[dict]:
    return [
        {"key": key, "name": label, "configured": _is_configured(key)}
        for key, (_fn, label) in CHANNELS.items()
    ]
