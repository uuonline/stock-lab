"""消息推送渠道。

支持：群晖 Chat、企业微信机器人、Telegram、Server酱、Bark、通用 Webhook。
全部通过 .env 配置，留空即自动跳过该渠道。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("stocklab.notify")

TIMEOUT = 15.0


def _post(url: str, verify: bool = True, **kwargs: Any) -> bool:
    """verify=False 用于群晖这类自签名证书的场景。

    ⚠️ 关闭校验只影响本机局域网内的 NAS 地址；不要拿它去访问公网服务。
    """
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, verify=verify) as c:
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


def send_synology_chat(content: str, title: str = "") -> bool:
    """群晖 Chat（Synology Chat）传入 Webhook。

    协议要点（和常见 webhook 不一样，容易踩坑）：
      · 地址形如 https://nas:port/webapi/entry.cgi?api=SYNO.Chat.External
        &method=incoming&version=2&token=xxx
      · 官方要求 **form-urlencoded**，JSON 放在 `payload` 字段里，
        而不是直接 POST 一个 JSON body
      · 群晖默认自签名证书，所以默认不校验 SSL（可用
        SL_NOTIFY_SYNOLOGY_VERIFY=1 打开严格校验）
    """
    url = settings.notify_synology_chat
    if not url:
        return False
    text = f"{title}\n{content}" if title else content
    return _post_synology(url, text, settings.notify_synology_verify)


# 群晖 Chat 的限流提示。实测连续发帖会返回
#   {"error":{"code":411,"errors":"create post too fast"}}
# 这是**软限流**：等几秒再发就成功。如果不重试，
# 多条提醒同时触发时后面的会被静默丢弃 —— 用户以为没触发，实际是没发出去。
_SYNOLOGY_BUSY = ("create post too fast", '"code":411')


def _post_synology(url: str, text: str, verify: bool,
                   attempts: int = 4, first_wait: float = 3.0) -> bool:
    import json as _json
    import time as _time
    payload = _json.dumps({"text": text}, ensure_ascii=False)
    wait = first_wait
    for i in range(attempts):
        try:
            with httpx.Client(timeout=TIMEOUT, trust_env=False, verify=verify) as c:
                r = c.post(url, data={"payload": payload})
                body = r.text or ""
            if r.status_code < 400 and "success" in body and '"success":true' in body:
                return True
            if any(k in body for k in _SYNOLOGY_BUSY):
                if i < attempts - 1:
                    log.info("群晖 Chat 限流，%.0f 秒后重试（第 %d 次）", wait, i + 1)
                    _time.sleep(wait)
                    wait *= 1.8
                    continue
                log.warning("群晖 Chat 持续限流，放弃本次推送")
                return False
            # 非限流失败：试一次 JSON body（部分版本接受）
            if i == 0:
                with httpx.Client(timeout=TIMEOUT, trust_env=False, verify=verify) as c:
                    r2 = c.post(url, json={"text": text})
                if r2.status_code < 400 and '"success":true' in (r2.text or ""):
                    return True
            log.warning("群晖 Chat 推送失败 HTTP %s %s", r.status_code, body[:150])
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("群晖 Chat 推送异常: %s", exc)
            return False
    return False


CHANNELS = {
    "synology": (send_synology_chat, "群晖 Chat"),
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
        "synology": bool(settings.notify_synology_chat),
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
