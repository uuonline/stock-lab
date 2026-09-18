"""Alpha Vantage 数据源。

实测能力（2026-09 验证）：
    美股 行情/日线/财务   ✅ 完整
    A股  行情/日线        ✅ 可用（沪 .SHH / 深 .SHZ）
    A股  财务             ❌ OVERVIEW 返回空
    港股                  ❌ 不支持

⚠️ 关键约束：**一次请求只能查一只标的**（不像东财/腾讯能批量）。
   免费额度很紧，所以本模块做了三件事来防止把额度烧光：
     1. 每日额度计数器（存 SQLite），超限直接拒绝，不静默失败
     2. 激进缓存：行情默认 5 分钟、日线 6 小时
     3. 只作为最后兜底 —— 默认不在 SL_SOURCE_ORDER 里占前位

因此在默认配置下，只有在东财/腾讯/新浪全部失败时才会消耗额度。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import db
from ..config import settings
from ..symbols import normalize
from .base import FetchError, fetch_json, to_float

log = logging.getLogger("stocklab.alphavantage")

BASE = "https://www.alphavantage.co/query"

# 支持的市场
SUPPORTED_MARKETS = {"SH", "SZ", "US"}


def to_av_symbol(symbol: str) -> str | None:
    """StockLab 代码 -> Alpha Vantage 代码。

    实测：沪市用 .SHH、深市用 .SHZ（不是 .SS/.SZ）。
    港股与北交所不支持，返回 None。
    """
    code, _, mkt = symbol.upper().rpartition(".")
    if mkt == "SH":
        return f"{code}.SHH"
    if mkt == "SZ":
        return f"{code}.SHZ"
    if mkt == "US":
        return code
    return None


def is_supported(symbol: str) -> bool:
    try:
        sym = normalize(symbol)
    except Exception:  # noqa: BLE001
        return False
    return sym.rpartition(".")[2] in SUPPORTED_MARKETS and bool(to_av_symbol(sym))


# ---------------- 每日额度控制 ----------------

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_today() -> int:
    """当日已用额度。兼容旧格式（纯数字）与新格式（dict）。"""
    v = db.kv_get(f"av_usage:{_today()}")
    if isinstance(v, dict):
        try:
            return int(v.get("count") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _bump_usage(n: int = 1) -> None:
    today = _today()
    key = f"av_usage:{today}"
    cur = usage_today()
    db.kv_set(key, {"count": cur + n, "at": time.strftime("%H:%M:%S")})


def quota_left() -> int:
    return max(0, settings.av_daily_limit - usage_today())


def _ensure_quota(need: int = 1) -> None:
    """额度不足时直接拒绝。

    宁可明确失败，也不要静默把额度耗光 —— 否则用户会以为源"坏了"，
    实际是当天额度用完了。
    """
    if not settings.av_api_key:
        raise FetchError("未配置 SL_ALPHAVANTAGE_KEY")
    left = quota_left()
    if left < need:
        raise FetchError(
            f"Alpha Vantage 当日额度不足（剩余 {left}，需要 {need}，"
            f"上限 {settings.av_daily_limit}；可调 SL_ALPHAVANTAGE_DAILY_LIMIT）"
        )


def _limit_message(payload: dict) -> str:
    for k in ("Note", "Information", "Error Message"):
        v = str(payload.get(k) or "")
        if v:
            return v
    return ""


def _classify_limit(msg: str) -> str | None:
    """区分「每日额度用尽」和「每秒突发超限」。

    实测官方文案：
      "Please consider spreading out your free API requests more sparingly
       (1 request per second). ... the free key rate limit (25 requests per day)"

    两者必须区别对待 —— 曾经把突发超限误判成当日额度用尽，
    结果一次连发就把整个源废掉了（真事）。
    """
    if not msg:
        return None
    low = msg.lower()

    # 顺序很关键：官方的**突发**超限文案里同时包含 "per second" 和
    # "per day"（后者是顺带提额度说明），先判 "per day" 会把突发误判成
    # 当日用尽 —— 这正是踩过的坑。所以先判更具体的突发特征。
    if any(k in low for k in ("sparingly", "per second", "burst", "frequency")):
        return "burst"
    if any(k in low for k in ("per day", "per 24", "daily")):
        return "daily"
    if "premium" in low or "limit" in low:
        return "burst"        # 无法判断时按轻的处理，避免误伤整天
    return None


_last_call = [0.0]


def _throttle_wait() -> None:
    """保证两次请求之间至少间隔 settings.av_min_interval 秒。

    官方免费额度是「每秒 1 次」，实测连发第 2 次就会被挡。
    """
    gap = settings.av_min_interval
    if gap <= 0:
        return
    delta = time.monotonic() - _last_call[0]
    if delta < gap:
        time.sleep(gap - delta)
    _last_call[0] = time.monotonic()


def _get(params: dict[str, Any], cache_ttl: float, max_burst_retry: int = 4) -> dict:
    _ensure_quota(1)
    attempt = 0
    while True:
        _throttle_wait()
        payload = fetch_json(
            BASE,
            params={**params, "apikey": settings.av_api_key},
            cache_ttl=cache_ttl,
            retries=2,
        )
        if not isinstance(payload, dict):
            raise FetchError(f"Alpha Vantage 返回异常结构: {type(payload).__name__}")

        # 正常数据
        if payload.get("Global Quote") is not None or payload.get("Time Series (Daily)") is not None:
            _bump_usage(1)
            return payload

        kind = _classify_limit(_limit_message(payload))
        if kind == "daily":
            # 真正用尽：把当天标记为满，避免继续无效请求
            _bump_usage(settings.av_daily_limit)
            raise FetchError("Alpha Vantage 当日额度已用尽（免费版 25 次/天）")
        if kind == "burst":
            attempt += 1
            if attempt > max_burst_retry:
                raise FetchError(
                    f"Alpha Vantage 连续 {max_burst_retry} 次触发每秒限流，暂时放弃"
                )
            wait = settings.av_burst_wait * attempt
            log.warning("Alpha Vantage 每秒限流，等待 %.1fs 后重试（第 %d 次）", wait, attempt)
            time.sleep(wait)
            continue
        _bump_usage(1)        # 有响应但不是数据，也算消耗
        return payload


# ---------------- 行情 ----------------

def quote_one(symbol: str) -> dict | None:
    """单只实时行情。注意：**每只消耗 1 次额度**。"""
    sym = normalize(symbol)
    av = to_av_symbol(sym)
    if not av:
        return None
    payload = _get(
        {"function": "GLOBAL_QUOTE", "symbol": av},
        cache_ttl=settings.av_quote_cache_ttl,
    )
    q = payload.get("Global Quote") or {}
    price = to_float(q.get("05. price"))
    if price is None:
        return None
    prev = to_float(q.get("08. previous close"))
    return {
        "symbol": sym,
        "code": sym.rpartition(".")[0],
        "name": "",                      # GLOBAL_QUOTE 不含名称
        "price": price,
        "open": to_float(q.get("02. open")),
        "high": to_float(q.get("03. high")),
        "low": to_float(q.get("04. low")),
        "prev_close": prev,
        "change": to_float(q.get("09. change")),
        "pct_change": to_float((q.get("10. change percent") or "").rstrip("%")),
        # 成交量单位是「股」，换算成「手」与其它源保持一致
        "volume": (lambda v: v / 100.0 if v is not None else None)(
            to_float(q.get("06. volume"))
        ),
        "update_time": q.get("07. latest trading day") or "",
        "_source": "alphavantage",
    }


def quotes(symbols: list[str], retries: int | None = None,
           timeout: float | None = None) -> dict[str, dict]:
    """批量行情 —— 但内部是逐只请求，**每只消耗 1 次额度**。

    额度不足时抛 FetchError，由上层决定是否降级。
    """
    out: dict[str, dict] = {}
    supported = [s for s in symbols if is_supported(s)]
    if not supported:
        return out
    _ensure_quota(len(supported))          # 一次性检查，避免查一半才失败
    for s in supported:
        try:
            q = quote_one(s)
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("Alpha Vantage 行情失败 %s: %s", s, exc)
            continue
        if q:
            out[normalize(s)] = q
        # 节流由 _get 内部的 _throttle_wait 统一处理，这里不再单独 sleep
    return out


# ---------------- K线 ----------------

def kline(symbol: str, period: str = "day", limit: int = 320) -> list[dict]:
    """日线。目前只支持日线（周/月线需另购权限，实测免费版不可用）。

    每次调用消耗 1 次额度；结果缓存 6 小时。
    """
    sym = normalize(symbol)
    av = to_av_symbol(sym)
    if not av:
        raise ValueError(f"Alpha Vantage 不支持该标的: {sym}")
    if period not in ("day", "daily"):
        raise ValueError(f"Alpha Vantage 仅支持日线，收到: {period}")

    payload = _get(
        {
            "function": "TIME_SERIES_DAILY",
            "symbol": av,
            "outputsize": "full" if limit > 100 else "compact",
        },
        cache_ttl=settings.av_kline_cache_ttl,
    )
    ts = payload.get("Time Series (Daily)") or {}
    if not ts:
        return []

    bars = []
    for day, ohlc in ts.items():
        bars.append({
            "date": day,
            "open": to_float(ohlc.get("1. open")),
            "high": to_float(ohlc.get("2. high")),
            "low": to_float(ohlc.get("3. low")),
            "close": to_float(ohlc.get("4. close")),
            "volume": (lambda v: v / 100.0 if v is not None else None)(
                to_float(ohlc.get("5. volume"))
            ),
            "amount": None,
        })
    bars.sort(key=lambda b: b["date"])
    if limit and len(bars) > limit:
        bars = bars[-limit:]
    return bars


def status() -> dict[str, Any]:
    return {
        "configured": bool(settings.av_api_key),
        "daily_limit": settings.av_daily_limit,
        "used_today": usage_today(),
        "left_today": quota_left(),
        "supported_markets": sorted(SUPPORTED_MARKETS),
    }
