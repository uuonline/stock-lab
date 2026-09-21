"""技术指标计算库。

刻意不依赖 TA-Lib：群晖 NAS 上没有编译工具链，pip 装 TA-Lib 必然失败。
这里用 numpy 纯实现，速度足够（单只股票 5000 根日线毫秒级）。

指标口径遵循国内行情软件（通达信/同花顺）惯例：
  * MACD 柱 = 2 * (DIF - DEA)
  * KDJ 使用 SMA(X, N, M) 平滑，等价于 alpha = M/N 的 EWM
  * RSI 使用 Wilder 平滑
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np


# ---------------- 基础工具 ----------------

def _arr(values: Sequence[float | None]) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=float)
    for i, v in enumerate(values):
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isnan(f):
            out[i] = f
    return out


def sma(values: Sequence[float], n: int) -> np.ndarray:
    """简单移动平均，前 n-1 个为 NaN。"""
    a = _arr(values)
    if n <= 0 or len(a) < n:
        return np.full(len(a), np.nan)
    out = np.full(len(a), np.nan)
    csum = np.cumsum(np.nan_to_num(a, nan=0.0))
    valid = ~np.isnan(a)
    for i in range(n - 1, len(a)):
        window = a[i - n + 1 : i + 1]
        if np.isnan(window).any():
            continue
        out[i] = window.mean()
    return out


def ema(values: Sequence[float], n: int) -> np.ndarray:
    a = _arr(values)
    out = np.full(len(a), np.nan)
    if n <= 0 or len(a) == 0:
        return out
    alpha = 2.0 / (n + 1.0)
    prev = np.nan
    for i, v in enumerate(a):
        if math.isnan(v):
            continue
        prev = v if math.isnan(prev) else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def cn_sma(values: Sequence[float], n: int, m: int) -> np.ndarray:
    """通达信 SMA(X, N, M)：Y = (M*X + (N-M)*Y') / N。"""
    a = _arr(values)
    out = np.full(len(a), np.nan)
    if n <= 0:
        return out
    alpha = m / float(n)
    prev = np.nan
    for i, v in enumerate(a):
        if math.isnan(v):
            continue
        prev = v if math.isnan(prev) else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def wilder(values: Sequence[float], n: int) -> np.ndarray:
    """Wilder 平滑（RSI/ATR 用）。"""
    a = _arr(values)
    out = np.full(len(a), np.nan)
    if len(a) < n or n <= 0:
        return out
    first = np.nanmean(a[:n]) if not np.isnan(a[:n]).all() else np.nan
    if math.isnan(first):
        return out
    out[n - 1] = first
    prev = first
    for i in range(n, len(a)):
        v = a[i]
        if math.isnan(v):
            out[i] = prev
            continue
        prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def hhv(values: Sequence[float], n: int) -> np.ndarray:
    a = _arr(values)
    out = np.full(len(a), np.nan)
    for i in range(len(a)):
        if i + 1 < n:
            continue
        w = a[i - n + 1 : i + 1]
        out[i] = np.nanmax(w) if not np.isnan(w).all() else np.nan
    return out


def llv(values: Sequence[float], n: int) -> np.ndarray:
    a = _arr(values)
    out = np.full(len(a), np.nan)
    for i in range(len(a)):
        if i + 1 < n:
            continue
        w = a[i - n + 1 : i + 1]
        out[i] = np.nanmin(w) if not np.isnan(w).all() else np.nan
    return out


def stddev(values: Sequence[float], n: int) -> np.ndarray:
    a = _arr(values)
    out = np.full(len(a), np.nan)
    for i in range(n - 1, len(a)):
        w = a[i - n + 1 : i + 1]
        if np.isnan(w).any():
            continue
        out[i] = w.std(ddof=0)  # 总体标准差，与通达信 STD 一致
    return out


def _clean(values: np.ndarray) -> list[float | None]:
    return [None if (v is None or math.isnan(v)) else round(float(v), 4) for v in values]


def arr_or_nan(values: Any) -> np.ndarray:
    """把 list[float|None] / list / ndarray 统一成 float ndarray（None -> NaN）。"""
    if isinstance(values, np.ndarray):
        a = values.astype(float, copy=True)
        return a
    return _arr(list(values))


# ---------------- 具体指标 ----------------

def ma(closes: Sequence[float], periods: Sequence[int] = (5, 10, 20, 60)) -> dict[str, list]:
    return {f"ma{p}": _clean(sma(closes, p)) for p in periods}


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, list]:
    dif = ema(closes, fast) - ema(closes, slow)
    dea = ema(_clean(dif), signal)
    hist = 2.0 * (dif - dea)
    return {"dif": _clean(dif), "dea": _clean(dea), "macd": _clean(hist)}


def kdj(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
    n: int = 9, m1: int = 3, m2: int = 3,
) -> dict[str, list]:
    hh = hhv(highs, n)
    ll = llv(lows, n)
    c = _arr(closes)
    span = hh - ll
    rsv = np.full(len(c), np.nan)
    for i in range(len(c)):
        if math.isnan(span[i]) or math.isnan(c[i]):
            continue
        rsv[i] = 50.0 if span[i] == 0 else (c[i] - ll[i]) / span[i] * 100.0
    k = cn_sma(_clean(rsv), m1, 1)
    d = cn_sma(_clean(k), m2, 1)
    j = 3.0 * k - 2.0 * d
    return {"k": _clean(k), "d": _clean(d), "j": _clean(j)}


def rsi(closes: Sequence[float], periods: Sequence[int] = (6, 12, 24)) -> dict[str, list]:
    a = _arr(closes)
    diff = np.full(len(a), np.nan)
    diff[1:] = a[1:] - a[:-1]
    gain = np.where(np.isnan(diff), np.nan, np.maximum(diff, 0))
    loss = np.where(np.isnan(diff), np.nan, np.maximum(-diff, 0))
    out: dict[str, list] = {}
    for p in periods:
        ag = wilder(_clean(gain), p)
        al = wilder(_clean(loss), p)
        r = np.full(len(a), np.nan)
        for i in range(len(a)):
            if math.isnan(ag[i]) or math.isnan(al[i]):
                continue
            r[i] = 100.0 if al[i] == 0 else 100.0 - 100.0 / (1.0 + ag[i] / al[i])
        out[f"rsi{p}"] = _clean(r)
    return out


def boll(closes: Sequence[float], n: int = 20, k: float = 2.0) -> dict[str, list]:
    mid = sma(closes, n)
    sd = stddev(closes, n)
    return {
        "mid": _clean(mid),
        "upper": _clean(mid + k * sd),
        "lower": _clean(mid - k * sd),
    }


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14
) -> list[float | None]:
    h, l, c = _arr(highs), _arr(lows), _arr(closes)
    tr = np.full(len(h), np.nan)
    for i in range(len(h)):
        if i == 0:
            tr[i] = h[i] - l[i] if not (math.isnan(h[i]) or math.isnan(l[i])) else np.nan
            continue
        pc = c[i - 1]
        if math.isnan(pc):
            tr[i] = h[i] - l[i]
            continue
        tr[i] = np.nanmax([h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc)])
    return _clean(wilder(_clean(tr), n))


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float | None]:
    c, v = _arr(closes), _arr(volumes)
    out = np.full(len(c), np.nan)
    prev = 0.0
    for i in range(len(c)):
        if math.isnan(c[i]) or math.isnan(v[i]):
            out[i] = prev
            continue
        if i == 0 or math.isnan(c[i - 1]):
            prev = v[i]
        elif c[i] > c[i - 1]:
            prev += v[i]
        elif c[i] < c[i - 1]:
            prev -= v[i]
        out[i] = prev
    return _clean(out)


def cci(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14
) -> list[float | None]:
    h, l, c = _arr(highs), _arr(lows), _arr(closes)
    tp = (h + l + c) / 3.0
    matp = sma(_clean(tp), n)
    out = np.full(len(c), np.nan)
    for i in range(n - 1, len(c)):
        w = tp[i - n + 1 : i + 1]
        m = matp[i]
        if math.isnan(m) or np.isnan(w).any():
            continue
        md = np.abs(w - m).mean()
        out[i] = 0.0 if md == 0 else (tp[i] - m) / (0.015 * md)
    return _clean(out)


def wr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14
) -> list[float | None]:
    hh, ll = hhv(highs, n), llv(lows, n)
    c = _arr(closes)
    out = np.full(len(c), np.nan)
    for i in range(len(c)):
        span = hh[i] - ll[i]
        if math.isnan(span) or math.isnan(c[i]) or span == 0:
            continue
        out[i] = (hh[i] - c[i]) / span * 100.0
    return _clean(out)


def boll_bandwidth(closes: Sequence[float], n: int = 20, k: float = 2.0) -> list[float | None]:
    b = boll(closes, n, k)
    mid = _arr(b["mid"])
    up = _arr(b["upper"])
    low = _arr(b["lower"])
    out = np.full(len(mid), np.nan)
    mask = (~np.isnan(mid)) & (mid != 0)
    out[mask] = (up[mask] - low[mask]) / mid[mask] * 100.0
    return _clean(out)


# ---------------- 信号识别 ----------------

def cross_over(a: Sequence[float], b: Sequence[float], lookback: int = 1) -> list[bool]:
    """a 上穿 b。"""
    x, y = _arr(a), _arr(b)
    res = [False] * len(x)
    for i in range(1, len(x)):
        if any(math.isnan(v) for v in (x[i], y[i], x[i - 1], y[i - 1])):
            continue
        res[i] = x[i - 1] <= y[i - 1] and x[i] > y[i]
    return res


def cross_under(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    x, y = _arr(a), _arr(b)
    res = [False] * len(x)
    for i in range(1, len(x)):
        if any(math.isnan(v) for v in (x[i], y[i], x[i - 1], y[i - 1])):
            continue
        res[i] = x[i - 1] >= y[i - 1] and x[i] < y[i]
    return res


def compute_all(bars: list[dict]) -> dict[str, Any]:
    """一次性算出全部指标，供图表与信号使用。"""
    if not bars:
        return {}
    closes = [b.get("close") for b in bars]
    highs = [b.get("high") for b in bars]
    lows = [b.get("low") for b in bars]
    vols = [b.get("volume") for b in bars]
    dates = [b.get("date") for b in bars]

    ind: dict[str, Any] = {"dates": dates}
    ind.update(ma(closes, (5, 10, 20, 30, 60, 120, 250)))
    ind.update(macd(closes))
    ind.update(kdj(highs, lows, closes))
    ind.update(rsi(closes))
    ind.update(boll(closes))
    ind["atr14"] = atr(highs, lows, closes, 14)
    ind["obv"] = obv(closes, vols)
    ind["cci14"] = cci(highs, lows, closes, 14)
    ind["wr14"] = wr(highs, lows, closes, 14)
    ind["boll_bw"] = boll_bandwidth(closes)
    ind["vol_ma5"] = _clean(sma([v or 0 for v in vols], 5))
    ind["vol_ma10"] = _clean(sma([v or 0 for v in vols], 10))
    return ind


def latest_snapshot(
    bars: list[dict], ind: dict[str, Any] | None = None
) -> dict[str, Any]:
    """最后一根 K 线上的指标值与多空评分。

    ind: 已经算好的 compute_all 结果，传入可省掉一次全量重算。
         实测 600 根 K线上 compute_all 要 347ms（NAS 的 J4125），
         而详情页本来就会调 compute_all —— 不传就是白烧一倍 CPU。
         本函数只读 ind，不会修改它，所以传进来是安全的。
    """
    if not bars or len(bars) < 2:
        return {}
    if ind is None:
        ind = compute_all(bars)
    n = len(bars)

    def last(key: str):
        seq = ind.get(key) or []
        return seq[n - 1] if len(seq) >= n else None

    def prev(key: str):
        seq = ind.get(key) or []
        return seq[n - 2] if len(seq) >= n else None

    close = bars[-1].get("close")
    prev_close = bars[-2].get("close")
    vol = bars[-1].get("volume") or 0
    vol_ma5 = last("vol_ma5")

    signals: list[dict[str, str]] = []
    score = 0

    # 均线多空
    ma5, ma10, ma20, ma60 = last("ma5"), last("ma10"), last("ma20"), last("ma60")
    if all(v is not None for v in (ma5, ma10, ma20)):
        if ma5 > ma10 > ma20:
            signals.append({"type": "bull", "text": "均线多头排列(MA5>MA10>MA20)"}); score += 2
        elif ma5 < ma10 < ma20:
            signals.append({"type": "bear", "text": "均线空头排列(MA5<MA10<MA20)"}); score -= 2
    if close is not None and ma20 is not None:
        if close > ma20:
            score += 1
        else:
            score -= 1
    if close is not None and ma60 is not None:
        score += 1 if close > ma60 else -1

    # MACD
    dif, dea = last("dif"), last("dea")
    pdif, pdea = prev("dif"), prev("dea")
    if None not in (dif, dea, pdif, pdea):
        if pdif <= pdea and dif > dea:
            signals.append({"type": "bull", "text": "MACD 金叉"}); score += 2
        elif pdif >= pdea and dif < dea:
            signals.append({"type": "bear", "text": "MACD 死叉"}); score -= 2
        if dif > 0 and dea > 0:
            score += 1
        elif dif < 0 and dea < 0:
            score -= 1

    # KDJ
    k, d, j = last("k"), last("d"), last("j")
    pk, pd_ = prev("k"), prev("d")
    if None not in (k, d, pk, pd_):
        if pk <= pd_ and k > d:
            signals.append({"type": "bull", "text": "KDJ 金叉"}); score += 1
        elif pk >= pd_ and k < d:
            signals.append({"type": "bear", "text": "KDJ 死叉"}); score -= 1
    if j is not None:
        if j < 0:
            signals.append({"type": "bull", "text": "KDJ 超卖(J<0)"}); score += 1
        elif j > 100:
            signals.append({"type": "bear", "text": "KDJ 超买(J>100)"}); score -= 1

    # RSI
    r6 = last("rsi6")
    if r6 is not None:
        if r6 < 20:
            signals.append({"type": "bull", "text": f"RSI6 超卖({r6})"}); score += 1
        elif r6 > 80:
            signals.append({"type": "bear", "text": f"RSI6 超买({r6})"}); score -= 1

    # 布林
    up, mid, lowb = last("upper"), last("mid"), last("lower")
    if close is not None and up is not None and lowb is not None:
        if close > up:
            signals.append({"type": "bear", "text": "价格突破布林上轨"}); score -= 1
        elif close < lowb:
            signals.append({"type": "bull", "text": "价格跌破布林下轨"}); score += 1

    # 量能
    vol_ratio = None
    if vol_ma5:
        vol_ratio = round(vol / vol_ma5, 2)
        if vol_ratio >= 2 and close is not None and prev_close is not None and close > prev_close:
            signals.append({"type": "bull", "text": f"放量上涨(量比{vol_ratio})"}); score += 2
        elif vol_ratio >= 2 and close is not None and prev_close is not None and close < prev_close:
            signals.append({"type": "bear", "text": f"放量下跌(量比{vol_ratio})"}); score -= 2

    if score >= 5:
        rating = "强烈偏多"
    elif score >= 2:
        rating = "偏多"
    elif score <= -5:
        rating = "强烈偏空"
    elif score <= -2:
        rating = "偏空"
    else:
        rating = "中性"

    return {
        "score": score,
        "rating": rating,
        "signals": signals,
        "vol_ratio_5": vol_ratio,
        "values": {
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma30": last("ma30"),
            "ma60": ma60, "ma120": last("ma120"), "ma250": last("ma250"),
            "dif": dif, "dea": dea, "macd": last("macd"),
            "k": k, "d": d, "j": j,
            "rsi6": r6, "rsi12": last("rsi12"), "rsi24": last("rsi24"),
            "boll_upper": up, "boll_mid": mid, "boll_lower": lowb,
            "atr14": last("atr14"), "cci14": last("cci14"), "wr14": last("wr14"),
        },
    }
