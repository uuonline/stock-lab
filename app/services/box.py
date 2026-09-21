"""箱体分析。

箱体（trading range / box）指的是股价在一段水平区间内反复震荡，
上沿构成压力（箱顶）、下沿构成支撑（箱底）。

设计要点 —— 最重要的不是画线，而是**先判断到底是不是箱体**：

    趋势行情里硬画一个箱体是误导。所以本模块先做「形态判定」，
    用线性回归斜率 + 价格穿越中轴次数来区分：
      · 震荡箱体 —— 斜率接近 0，价格多次穿越中轴
      · 上升通道 —— 斜率显著为正
      · 下降通道 —— 斜率显著为负
    只有震荡箱体才给出「箱底/箱顶」的区间操作参考。

边界取法上刻意用**分位数**而不是最高/最低价：
    单根插针（比如某天闪崩又拉回）会把 min/max 拉得很远，
    画出来的箱体失去意义。分位数能抗这种噪声。
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("stocklab.box")

# 默认观察窗口（交易日）
DEFAULT_WINDOW = 60
# 可用窗口
WINDOWS = (30, 60, 120, 250)


def _pct(sorted_vals: list[float], p: float) -> float:
    """分位数（线性插值）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(math.floor(k))
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _linreg(ys: list[float]) -> tuple[float, float]:
    """对 y 按序号做一元线性回归，返回 (斜率, R²)。"""
    n = len(ys)
    if n < 3:
        return 0.0, 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    # R²
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return slope, max(0.0, min(1.0, r2))


def analyze(
    bars: list[dict],
    window: int = DEFAULT_WINDOW,
    price: float | None = None,
) -> dict[str, Any] | None:
    """对最近 window 根 K线做箱体分析。

    返回 None 表示数据不足。
    """
    if not bars or len(bars) < 20:
        return None
    seg = bars[-window:] if len(bars) > window else list(bars)
    n = len(seg)

    highs = sorted(b["high"] for b in seg if b.get("high"))
    lows = sorted(b["low"] for b in seg if b.get("low"))
    closes = [b["close"] for b in seg if b.get("close")]
    if len(highs) < 10 or len(lows) < 10 or len(closes) < 10:
        return None

    cur = price if price is not None else closes[-1]
    if not cur:
        return None

    # ---- 箱顶/箱底：分位数抗插针 ----
    # 先取 90/10 分位，再检查是否覆盖足够多的收盘价，
    # 覆盖不足就逐步放宽（避免箱体太窄导致大量价格在箱外）
    top = _pct(highs, 90)
    bottom = _pct(lows, 10)
    for widen in (0.0, 0.03, 0.06, 0.10):
        t = _pct(highs, min(98.0, 90.0 + widen * 100))
        b = _pct(lows, max(2.0, 10.0 - widen * 100))
        inside = sum(1 for c in closes if b <= c <= t)
        if inside / len(closes) >= 0.85:
            top, bottom = t, b
            break

    if top <= bottom:
        return None

    height = top - bottom
    height_pct = height / bottom * 100.0

    # ---- 趋势强度 ----
    slope, r2 = _linreg(closes)
    mean_close = sum(closes) / len(closes)
    slope_pct = (slope / mean_close * 100.0) if mean_close else 0.0   # 每日涨跌 %

    # ---- 穿越中轴次数（震荡的特征）----
    mid = (top + bottom) / 2.0
    crosses = 0
    for i in range(1, n):
        if (closes[i - 1] - mid) * (closes[i] - mid) < 0:
            crosses += 1
    crosses_per_10 = crosses / n * 10.0

    # ---- 触碰箱顶/箱底的次数（验证边界是否被市场认可）----
    tol = height * 0.03          # 3% 容差
    touch_top = sum(1 for b in seg if (b.get("high") or 0) >= top - tol)
    touch_bottom = sum(1 for b in seg if (b.get("low") or 1e18) <= bottom + tol)

    # ---- 形态判定 ----
    # 每日斜率超过 0.15% 视为有趋势；穿越中轴频率低也说明在走趋势
    TREND_TH = 0.15
    if abs(slope_pct) < TREND_TH and crosses_per_10 >= 1.0:
        shape = "震荡箱体"
    elif slope_pct >= TREND_TH:
        shape = "上升通道"
    elif slope_pct <= -TREND_TH:
        shape = "下降通道"
    else:
        shape = "弱趋势整理"

    # 箱体有效性置信度：边界被触碰越多、穿越越频繁、斜率越小 → 越像箱体
    conf = 0.0
    conf += min(touch_top, 3) / 3 * 0.25
    conf += min(touch_bottom, 3) / 3 * 0.25
    conf += min(crosses_per_10 / 3.0, 1.0) * 0.25
    conf += max(0.0, 1.0 - abs(slope_pct) / (TREND_TH * 2)) * 0.25
    confidence = round(conf * 100)
    if shape != "震荡箱体":
        confidence = round(confidence * 0.6)      # 非震荡形态降低置信度

    # ---- 当前位置与状态 ----
    pos = (cur - bottom) / height * 100.0
    pos_clamped = max(0.0, min(100.0, pos))

    if cur > top:
        status = "向上突破箱顶"
    elif cur < bottom:
        status = "向下跌破箱底"
    else:
        status = "箱体内运行"

    # ---- 区间操作参考（技术位推算，非买卖建议）----
    if status == "箱体内运行":
        if pos_clamped <= 20:
            zone, note = "箱底区", "接近箱体下沿，历史上此处获得支撑的概率较高"
        elif pos_clamped >= 80:
            zone, note = "箱顶区", "接近箱体上沿，历史上此处遇到压力的概率较高"
        elif 40 <= pos_clamped <= 60:
            zone, note = "箱体中轴", "位于箱体中部，方向不明，上下空间相当"
        else:
            zone, note = "箱体偏中", "距箱体边界尚有空间"
    elif status == "向上突破箱顶":
        zone, note = "箱外(上)", "已突破箱顶；原箱顶通常转为支撑，若快速跌回箱内则为假突破"
    else:
        zone, note = "箱外(下)", "已跌破箱底；原箱底通常转为压力，需警惕趋势走弱"

    return {
        "window": window,
        "bars": n,
        "start_date": seg[0].get("date"),
        "end_date": seg[-1].get("date"),
        "top": round(top, 3),
        "bottom": round(bottom, 3),
        "mid": round(mid, 3),
        "height": round(height, 3),
        "height_pct": round(height_pct, 2),
        "price": round(cur, 3),
        "position_pct": round(pos_clamped, 1),
        "position_raw": round(pos, 1),
        "status": status,
        "shape": shape,
        "confidence": confidence,
        "touch_top": touch_top,
        "touch_bottom": touch_bottom,
        "crosses": crosses,
        "crosses_per_10": round(crosses_per_10, 2),
        "slope_pct": round(slope_pct, 3),
        "r2": round(r2, 3),
        "zone": zone,
        "note": note,
        # 便于前端画图：箱体在 K线数组中的起止索引
        "start_index": len(bars) - n,
        "end_index": len(bars) - 1,
    }


def analyze_multi(bars: list[dict], price: float | None = None) -> dict[str, Any]:
    """多窗口箱体对比，帮用户判断看哪个周期。

    短窗口灵敏但噪声大，长窗口稳定但滞后 —— 一起给出更实用。
    """
    out: dict[str, Any] = {}
    for w in WINDOWS:
        if len(bars) >= min(w, 20):
            r = analyze(bars, w, price)
            if r:
                out[str(w)] = r
    # 推荐窗口的选取规则（曾经这里用 sorted(out)[0] 兜底，
    # 但字符串排序会得到 120/250/30/60 这种字母序，等于随机挑一个）：
    #   1) 有震荡箱体 → 取置信度最高的那个
    #   2) 没有箱体   → 取置信度最高者；同分时偏好 60 日（经验上最常用）
    def score(item):
        w, r = item
        return (r["shape"] == "震荡箱体", r["confidence"], w == "60")

    best = max(out.items(), key=score)[0] if out else None
    return {"windows": out, "recommended": best}
