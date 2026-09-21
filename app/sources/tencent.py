"""腾讯行情数据源（备源 + 港股 K线主力）。

优点：稳定、不限速、覆盖 A股/港股/指数/ETF，K线支持 count 参数。
缺点：GBK 编码；行情字段为位置索引，需按位解析。
"""
from __future__ import annotations

from typing import Any

from ..symbols import to_tencent
from .base import FetchError, fetch_json, fetch_text, to_float

QUOTE_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
HK_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
MIN_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

PERIOD_MAP = {"day": "day", "week": "week", "month": "month"}
MIN_PERIOD_MAP = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


def _f(parts: list[str], idx: int, scale: float = 1.0) -> float | None:
    if idx >= len(parts):
        return None
    v = to_float(parts[idx])
    return None if v is None else v * scale


def quotes(symbols: list[str], retries: int | None = None,
           timeout: float | None = None, deadline: float | None = None) -> dict[str, dict]:
    """批量实时行情。腾讯单次建议 <= 60 个。"""
    out: dict[str, dict] = {}
    if not symbols:
        return out
    for i in range(0, len(symbols), 60):
        chunk = symbols[i : i + 60]
        codes = ",".join(to_tencent(s) for s in chunk)
        text = fetch_text(QUOTE_URL + codes, encoding="gbk", retries=retries,
                          timeout=timeout, cache_ttl=5)
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("v_"):
                continue
            head, _, payload = line.partition("=")
            key = head[2:]  # sh600519
            parts = payload.strip().strip('";').split("~")
            if len(parts) < 33 or not parts[2]:
                continue
            symbol = None
            for s in chunk:
                if to_tencent(s) == key:
                    symbol = s
                    break
            if symbol is None:
                continue
            amount_wan = _f(parts, 37)
            out[symbol] = {
                "symbol": symbol,
                "code": parts[2],
                "name": parts[1],
                "price": _f(parts, 3),
                "prev_close": _f(parts, 4),
                "open": _f(parts, 5),
                "volume": _f(parts, 6),                       # 手
                "change": _f(parts, 31),
                "pct_change": _f(parts, 32),
                "high": _f(parts, 33),
                "low": _f(parts, 34),
                "amount": None if amount_wan is None else amount_wan * 10000.0,  # 万元 -> 元
                "turnover_rate": _f(parts, 38),
                "pe_ttm": _f(parts, 39),
                "amplitude": _f(parts, 43),
                "float_cap": _f(parts, 44, 1e8),              # 亿元 -> 元
                "market_cap": _f(parts, 45, 1e8),
                "pb": _f(parts, 46),
                "limit_up": _f(parts, 47),
                "limit_down": _f(parts, 48),
                "vol_ratio": _f(parts, 49),
                "avg_price": _f(parts, 51),
                "pe": _f(parts, 52),
                "update_time": parts[30] if len(parts) > 30 else "",
                "_source": "tencent",
            }
    return out


def kline(
    symbol: str,
    period: str = "day",
    limit: int = 320,
    adjust: str = "qfq",
) -> list[dict]:
    """K线。period: 1m/5m/15m/30m/60m/day/week/month"""
    code = to_tencent(symbol)
    if period in MIN_PERIOD_MAP:
        return _minute_kline(code, MIN_PERIOD_MAP[period], limit)

    p = PERIOD_MAP.get(period, "day")
    is_hk = symbol.endswith(".HK")
    url = HK_KLINE_URL if is_hk else KLINE_URL
    param = f"{code},{p},,,{max(1, min(limit, 1500))},{adjust}"

    data = fetch_json(url, params={"param": param}, cache_ttl=900)
    node = (data.get("data") or {}).get(code) or {}
    series = None
    for key in (f"{adjust}{p}", p, "day"):
        if node.get(key):
            series = node[key]
            break
    if series is None:
        raise FetchError(f"腾讯无K线数据: {symbol} {period}")

    bars: list[dict] = []
    for row in series:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        # 腾讯顺序: [date, open, close, high, low, volume, ...]
        bars.append(
            {
                "date": str(row[0]),
                "open": to_float(row[1]),
                "close": to_float(row[2]),
                "high": to_float(row[3]),
                "low": to_float(row[4]),
                "volume": to_float(row[5]),
                "amount": to_float(row[8]) if len(row) > 8 else None,
            }
        )
    if limit and len(bars) > limit:
        bars = bars[-limit:]
    return bars


def _norm_time(raw: str) -> str:
    """腾讯分钟线返回 202609181500 这种 12 位数字，统一成 2026-09-18 15:00。

    不同数据源的分钟时间格式不一致（东财是 '2026-09-18 14:05'），
    混用会让图表横轴看起来很乱，所以在入口处归一化。
    """
    t = str(raw).strip()
    if len(t) == 12 and t.isdigit():
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
    if len(t) == 8 and t.isdigit():
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]}"
    return t


def _minute_kline(code: str, period: str, limit: int) -> list[dict]:
    data = fetch_json(
        MIN_KLINE_URL,
        params={"param": f"{code},{period},,{max(1, min(limit, 800))}"},
        cache_ttl=120,
    )
    node = (data.get("data") or {}).get(code) or {}
    series = node.get(period) or []
    bars = []
    for row in series:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        # 分钟线顺序: [time, price, volume, avg, ...] 或 [time, open, close, high, low, volume]
        if len(row) >= 6 and to_float(row[3]) is not None and len(str(row[0])) > 10:
            bars.append(
                {
                    "date": _norm_time(row[0]),
                    "open": to_float(row[1]),
                    "close": to_float(row[2]),
                    "high": to_float(row[3]),
                    "low": to_float(row[4]),
                    "volume": to_float(row[5]),
                    "amount": None,
                }
            )
        else:
            px = to_float(row[1])
            bars.append(
                {
                    "date": _norm_time(row[0]),
                    "open": px, "close": px, "high": px, "low": px,
                    "volume": to_float(row[2]), "amount": None,
                }
            )
    if limit and len(bars) > limit:
        bars = bars[-limit:]
    return bars


MINUTE_QUERY_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"


def minutes(symbol: str, retries: int | None = None,
            timeout: float | None = None) -> list[dict]:
    """当日分时（东财 trends2 的兜底源）。

    腾讯返回形如 "0930 1262.99 113 14271787.32"，其中**成交量与成交额是累计值**，
    不是每分钟的量。这里做差分还原成每分钟，否则分时图的量柱会单调递增、
    完全看不出放量缩量。
    """
    code = to_tencent(symbol)
    data = fetch_json(
        MINUTE_QUERY_URL, params={"code": code},
        retries=retries, timeout=timeout, cache_ttl=30,
    )
    node = ((data.get("data") or {}).get(code)) or {}
    inner = node.get("data") or {}
    rows = inner.get("data") or []
    date_raw = str(inner.get("date") or "")
    if len(date_raw) == 8 and date_raw.isdigit():
        date_str = f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
    else:
        date_str = date_raw

    out: list[dict] = []
    prev_vol = 0.0
    prev_amt = 0.0
    for line in rows:
        p = str(line).split()
        if len(p) < 2:
            continue
        hhmm = p[0]
        if len(hhmm) != 4 or not hhmm.isdigit():
            continue
        price = to_float(p[1])
        cum_vol = to_float(p[2]) if len(p) > 2 else None
        cum_amt = to_float(p[3]) if len(p) > 3 else None
        # 累计量差分；异常（变小）时按 0 处理，避免出现负量柱
        vol = None
        if cum_vol is not None:
            vol = cum_vol - prev_vol
            if vol < 0:
                vol = 0.0
            prev_vol = cum_vol
        amt = None
        if cum_amt is not None:
            amt = cum_amt - prev_amt
            if amt < 0:
                amt = 0.0
            prev_amt = cum_amt
        out.append({
            "time": f"{date_str} {hhmm[0:2]}:{hhmm[2:4]}" if date_str else f"{hhmm[0:2]}:{hhmm[2:4]}",
            "price": price,
            "open": price, "high": price, "low": price,
            "volume": None if vol is None else vol / 100.0,   # 股 -> 手
            "amount": amt,
            "avg": None,
        })
    return out
