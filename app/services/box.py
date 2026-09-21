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

    # ---- 箱内占比 ----
    inside_ratio = sum(1 for c in closes if bottom <= c <= top) / len(closes)

    # ---- 边界平整度：真箱体的上下沿应该是「躺平」的 ----
    # 把窗口三等分，各算一次上沿/下沿，看它们漂移了几个箱体高度。
    # 这是区分「水平箱体」和「斜着走的通道」最直接的量：
    # 通道的两条边是平行的斜线，三等分上沿会一路抬高。
    third = max(3, n // 3)
    parts = [seg[i:i + third] for i in range(0, n, third)][:3]
    part_tops, part_bottoms = [], []
    for p in parts:
        ph = sorted(b["high"] for b in p if b.get("high"))
        pl = sorted(b["low"] for b in p if b.get("low"))
        if ph and pl:
            part_tops.append(_pct(ph, 90))
            part_bottoms.append(_pct(pl, 10))
    top_drift = bot_drift = 0.0
    if len(part_tops) >= 2 and height > 0:
        top_drift = (part_tops[-1] - part_tops[0]) / height
        bot_drift = (part_bottoms[-1] - part_bottoms[0]) / height

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
        "inside_ratio": round(inside_ratio, 3),
        "top_drift": round(top_drift, 3),
        "bot_drift": round(bot_drift, 3),
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


# ============================================================
# 自适应交易日窗口
# ============================================================
#
# 固定窗口（30/60/120/250）有个根本问题：它用同一把尺子量所有股票。
# 但每只股票的运作节奏差很远 —— 有的 8 天一个来回，有的 40 天才走完一段。
# 窗口取得比节奏短，会看到一堆噪声；取得比节奏长，会把几段不同性质的
# 行情混成一个「箱体」，画出来的上下沿根本没有意义。
#
# 所以这里做两件事：
#   1) 用 zigzag 量出这只股票自己的波段节奏（几档阈值 → 嵌套的周期结构）
#   2) 在 20~250 天里逐窗扫描箱体质量，并且**取高分平台的中心**而不是
#      最高分那个点 —— 单点最优几乎必然是噪声拟合出来的尖峰，
#      真正有节奏的窗口，它左右邻居的分数也应该一样高。
#
# 关于「主力操作手法」的说明：这里能从数据里读出来的是**节奏**
# （波段长度、箱体持续天数），读不出「主力」的意图。节奏是客观的，
# 意图不是 —— 所以下面的结论只说节奏，不做任何主力意图的推断。

# 扫描范围与步长
SCAN_LO, SCAN_HI, SCAN_STEP = 20, 250, 5
# 判定「高分平台」的相对容差
PLATEAU_TOL = 0.05
# 箱体窗口应覆盖的完整往复数区间（2~3 个往复）
CYCLE_MIN, CYCLE_MAX = 1.8, 3.2
# 低于这个分数就认为「当前没有可信箱体」，如实告知而不是硬给结论
MIN_TRUST_SCORE = 0.55


def zigzag(vals: list[float], thr: float) -> list[int]:
    """经典 zigzag 转折点检测。

    涨跌幅度超过 thr 才确认一个转折，返回转折点的下标。

    实现上有个容易写错的地方：**只能在行进方向上更新极值**，
    反转判断要相对「还没被更新的」极值做。否则在方向未定时
    两个分支会互相顶替，极值跟着每根 K线跳，永远测不出回撤
    （第一版就踩了这个坑，641 根 K线只识别出 2 个转折点）。
    """
    n = len(vals)
    if n < 3:
        return []
    piv: list[int] = [0]
    direction = 0
    ext_i, ext_p = 0, vals[0]
    for i in range(1, n):
        c = vals[i]
        if direction == 0:
            if c >= ext_p * (1 + thr):
                direction, ext_i, ext_p = 1, i, c
            elif c <= ext_p * (1 - thr):
                direction, ext_i, ext_p = -1, i, c
        elif direction == 1:
            if c > ext_p:
                ext_i, ext_p = i, c
            elif c <= ext_p * (1 - thr):
                piv.append(ext_i)
                direction, ext_i, ext_p = -1, i, c
        else:
            if c < ext_p:
                ext_i, ext_p = i, c
            elif c >= ext_p * (1 + thr):
                piv.append(ext_i)
                direction, ext_i, ext_p = 1, i, c
    piv.append(ext_i)
    return sorted(set(piv))


MIN_LEGS = 6          # 少于这么多段，中位数不可信


# 节奏测量的固定回看长度。
# 必须固定：如果让它跟着「一共取了多少根 K线」变，同一只股票换个数据长度
# 就会得出不同的周期（实测 600519 在 641 根下算 56 天、800 根下算 70 天，
# 因为更早的历史里波段更长，把中位数拉大了）。固定回看长度后，
# 无论上层取 641 还是 800 根，节奏都在同一段近期数据上测量，结果可复现。
RHYTHM_LOOKBACK = 500


def swing_rhythm(
    bars: list[dict],
    thresholds: tuple[float, ...] = (0.03, 0.04, 0.05, 0.08, 0.10, 0.15),
    lookback: int = RHYTHM_LOOKBACK,
) -> dict[str, Any]:
    """量出这只股票自身的波段节奏。

    不同阈值对应不同级别的波动，得到的是**嵌套**结构：
    小阈值抓小波动（噪声级别），大阈值抓主升主跌的大波段。
    箱体最关心的是中等阈值那一档 —— 那才是「在一段区间里来回磨」的级别。
    """
    # 只取最近 lookback 根：节奏要反映"现在的手法"，不该被几年前的行情带偏，
    # 顺带让结果不随上层取多少历史而变。
    if lookback and len(bars) > lookback:
        bars = bars[-lookback:]
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < 60:
        return {"ok": False, "levels": [], "cycle_days": None,
                "note": "K线不足 60 根，无法测量节奏"}

    levels = []
    for thr in thresholds:
        piv = zigzag(closes, thr)
        gaps = [piv[i + 1] - piv[i] for i in range(len(piv) - 1)]
        if len(gaps) < MIN_LEGS:
            # 波段太少时中位数完全不可信。趋势股尤其容易这样：
        # 一路上涨只留下两三个大转折，算出来的"周期"其实是趋势长度。
            continue
        gaps_sorted = sorted(gaps)
        med = gaps_sorted[len(gaps_sorted) // 2]
        levels.append({
            "threshold_pct": round(thr * 100, 1),
            "legs": len(gaps),
            "median_leg_days": int(med),
            "mean_leg_days": round(sum(gaps) / len(gaps), 1),
            "min_leg_days": min(gaps),
            "max_leg_days": max(gaps),
            # 一个完整往复 = 上 + 下 ≈ 两段
            "cycle_days": int(med * 2),
        })

    if not levels:
        return {
            "ok": False, "levels": [], "cycle_days": None,
            "note": f"各档阈值下波段都不足 {MIN_LEGS} 段（该股走势过于单边或波动过小），"
                    f"测不出可信节奏",
        }

    # 中等波动那一档作为「震荡级别」的代表：
    # 太小是噪声，太大是趋势，都不是箱体要刻画的对象。
    mid = min(levels, key=lambda x: abs(x["threshold_pct"] - 10.0))
    return {
        "ok": True,
        "levels": levels,
        "mid_level": mid,
        "lookback": len(closes),
        "cycle_days": mid["cycle_days"],
        "note": (
            f"该股 {mid['threshold_pct']:.0f}% 级别波段的中位长度为 "
            f"{mid['median_leg_days']} 个交易日，一个完整往复约 "
            f"{mid['cycle_days']} 天"
        ),
    }


def box_quality(bars: list[dict], window: int, price: float | None = None) -> dict[str, Any] | None:
    """给「某个窗口内的箱体质量」打分，0~1。

    打分维度刻意覆盖箱体的各个必要条件，缺一项就明显掉分：
      flat     上下沿是否躺平（通道的边是斜的，会在这里露馅）
      touch    上下沿是否被反复触碰（没人碰的边不算边）
      osc      价格是否在区间里来回穿越中轴
      inside   收盘价落在箱内的比例
      notrend  斜率是否足够小
      sample   样本量是否足够
    """
    r = analyze(bars, window, price)
    if not r:
        return None

    height = r["height"] or 1e-9
    n = r["bars"]

    # 上下沿漂移：以一个箱体高度为尺度。
    # 这是区分「水平箱体」和「斜着的通道」最硬的指标 ——
    # 边界一路抬高的东西，无论触碰多少次都不是箱体。
    drift = abs(r["top_drift"]) + abs(r["bot_drift"])
    flat = max(0.0, 1.0 - drift / 0.6)

    # 震荡频率：价格必须反复穿越中轴。
    # 注意这一项不能用绝对触碰次数代替 —— 长窗口下触碰次数几乎自动变多，
    # 240 根里碰到 25 次上沿，和 70 根里碰到 10 次，密度其实差不多，
    # 但前者根本不是箱体（实测 flat=0、osc=0.21）。所以用密度而非计数。
    osc = min(r["crosses_per_10"] / 3.0, 1.0)

    # 触碰：两边都要碰到，各 2 次以上
    tt, tb = r["touch_top"], r["touch_bottom"]
    touch = (min(tt, 4) / 4) * 0.5 + (min(tb, 4) / 4) * 0.5
    if tt < 2 or tb < 2:
        touch *= 0.5

    inside = r["inside_ratio"]
    notrend = max(0.0, 1.0 - abs(r["slope_pct"]) / 0.30)
    sample = min(1.0, n / 60.0)

    # 箱体高度合理性：超过 20% 开始扣分，40% 归零。
    # 一个 37% 宽的"箱体"其实是趋势，不是区间。
    hp = r["height_pct"]
    height_ok = 1.0 if hp <= 20 else max(0.0, 1.0 - (hp - 20) / 20.0)

    raw = (0.28 * flat + 0.20 * osc + 0.15 * touch + 0.12 * inside
           + 0.12 * notrend + 0.08 * sample + 0.05 * height_ok)

    # 形态闸门：形态判定已经把「斜率 + 震荡」的领域知识编码进去了，
    # 不是震荡箱体就直接打对折 —— 否则趋势股靠"箱内占比高"也能拿高分。
    is_box = r["shape"] == "震荡箱体"
    score = raw * (1.0 if is_box else 0.55)

    return {
        "window": window,
        "score": round(score, 4),
        "parts": {
            "flat": round(flat, 3), "osc": round(osc, 3), "touch": round(touch, 3),
            "inside": round(inside, 3), "notrend": round(notrend, 3),
            "sample": round(sample, 3), "height_ok": round(height_ok, 3),
            "is_box": is_box,
        },
        "shape": r["shape"],
        "confidence": r["confidence"],
        "top": r["top"], "bottom": r["bottom"],
        "start_date": r["start_date"], "end_date": r["end_date"],
        "height_pct": r["height_pct"],
        "slope_pct": r["slope_pct"],
        "touch_top": tt, "touch_bottom": tb,
        "crosses_per_10": r["crosses_per_10"],
        "top_drift": r["top_drift"],
        "bot_drift": r["bot_drift"],
    }


def scan_windows(
    bars: list[dict],
    price: float | None = None,
    lo: int = SCAN_LO,
    hi: int = SCAN_HI,
    step: int = SCAN_STEP,
) -> list[dict[str, Any]]:
    """逐窗扫描箱体质量。至少需要 hi 根 K线才能扫满，不足则自动收窄。"""
    if not bars:
        return []
    hi = min(hi, len(bars))
    out = []
    for w in range(lo, hi + 1, step):
        q = box_quality(bars, w, price)
        if q:
            out.append(q)
    return out


def adaptive(bars: list[dict], price: float | None = None) -> dict[str, Any]:
    """按个股自身节奏推荐箱体窗口。

    分两步，顺序很重要：

    **第一步，用节奏定搜索范围。** 先量出这只股票的波段周期，箱体窗口
    应当覆盖 2~3 个完整往复 —— 少于 2 个往复看到的只是一段单边行情，
    多于 3 个又会把性质完全不同的几段行情混在一起。这一步把候选
    从 47 档收窄到节奏允许的那一段，是「按主力手法定天数」的落点。

    **第二步，在范围内按箱体质量选，并取高分平台的中心。**
    不用最高分那个点：47 档里最高分几乎总是一个孤立尖峰，那是拟合
    噪声拟合出来的。真正有节奏的窗口，左右邻居的分数也该一样高。

    两步互相印证：节奏说该多长、数据说哪段最像箱体，两者重合才可信。
    """
    rhythm = swing_rhythm(bars)
    curve = scan_windows(bars, price)
    if not curve:
        return {"ok": False, "reason": "K线不足，无法扫描窗口", "rhythm": rhythm}

    # ---- 第一步：节奏决定的窗口区间 ----
    #
    # 这一步不能省，也不能在推不出区间时"退回全范围扫描"。
    #
    # 原因：**分数不能跨窗口长度直接比较**。长窗口经历更多行情演变，
    # 上下沿漂移更多、穿越更少，天然吃亏；短窗口则相反。
    # 实测平安银行在节奏带失效、退回全范围后，选中的是 20 天窗口（4.1% 高度，
    # 分数 0.825）—— 一个刚上市两周的窄幅整理就能拿这个分。这不是用户在
    # 问的"按这只股票的节奏定期数"，而是短窗口占了尺度的便宜。
    #
    # 所以推不出节奏区间时，正确做法是**如实说推不出来**。
    band: dict[str, Any] = {"applied": False}
    cyc = rhythm.get("cycle_days") if rhythm.get("ok") else None

    def _abstain(reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "trustworthy": False,
            "verdict": reason,
            "recommended_window": None,
            "band": band,
            "plateau": None,
            "curve": [
                {"window": c["window"], "score": c["score"],
                 "flat": c["parts"]["flat"], "touch": c["parts"]["touch"]}
                for c in curve
            ],
            "rhythm": rhythm,
            "analysis": None,
            "fixed_best": None,
            "gain_vs_fixed": None,
            "reason": reason,
        }

    if not cyc:
        band = {"applied": False, "note": rhythm.get("note", "测不出节奏")}
        return _abstain(
            "测不出该股的可信波段节奏（走势过于单边或波动过小），"
            "因此无法按它的节奏推荐箱体窗口。此时任何固定天数都是拍脑袋，"
            "不如不给结论。"
        )

    lo_w = max(SCAN_LO, int(round(cyc * CYCLE_MIN)))
    hi_w = min(SCAN_HI, int(round(cyc * CYCLE_MAX)))
    narrowed = [c for c in curve if lo_w <= c["window"] <= hi_w]
    if len(narrowed) < 3:
        band = {"applied": False, "cycle_days": cyc,
                "note": f"节奏 {cyc} 天需要的窗口超出扫描上限 {SCAN_HI} 天"}
        return _abstain(
            f"该股节奏约 {cyc} 天一个往复，覆盖两个往复需要 {lo_w} 天以上的窗口，"
            f"已超出箱体可刻画的上限 {SCAN_HI} 天。"
            f"这种长周期通常对应趋势行情而非区间震荡，不适合用箱体做参考。"
        )

    pool = narrowed
    clamped = cyc * CYCLE_MAX > SCAN_HI
    note = (f"该股节奏为 {cyc} 天一个往复，箱体窗口取 2~3 个往复"
            f"即 {pool[0]['window']}~{pool[-1]['window']} 天")
    band = {
        "applied": True,
        "from": pool[0]["window"], "to": pool[-1]["window"],
        "cycle_days": cyc,
        "clamped": clamped,
        "note": note,
    }

    # ---- 第二步：在范围内取高分平台的中心 ----
    best_score = max(c["score"] for c in pool)
    thresh = best_score - PLATEAU_TOL

    runs: list[list[dict]] = []
    cur_run: list[dict] = []
    for c in pool:
        if c["score"] >= thresh:
            cur_run.append(c)
        else:
            if cur_run:
                runs.append(cur_run)
            cur_run = []
    if cur_run:
        runs.append(cur_run)

    plateau = max(runs, key=lambda r: (len(r), sum(x["score"] for x in r) / len(r)))
    chosen = plateau[len(plateau) // 2]

    # 与固定窗口对比：自适应是否真的更好
    fixed_pool = [c for c in curve if c["window"] in WINDOWS]
    fixed_best = max(fixed_pool, key=lambda c: c["score"]) if fixed_pool else None
    gain = round(chosen["score"] - fixed_best["score"], 4) if fixed_best else None

    result = analyze(bars, chosen["window"], price)

    # 可信度闸门：达不到最低分就如实说「现在没有像样的箱体」，
    # 而不是硬塞一个最不难看的窗口给用户当结论用。
    trustworthy = chosen["score"] >= MIN_TRUST_SCORE
    verdict = None
    if not trustworthy:
        verdict = (
            f"该股当前没有可信的箱体形态："
            f"{band.get('from', curve[0]['window'])}~{band.get('to', curve[-1]['window'])} "
            f"天范围内最高分仅 {best_score:.3f}（门槛 {MIN_TRUST_SCORE}）。"
            f"这通常意味着股价正在走趋势而不是区间震荡，"
            f"此时给出的上下沿参考价值有限。"
        )

    return {
        "ok": True,
        "trustworthy": trustworthy,
        "verdict": verdict,
        "recommended_window": chosen["window"],
        "band": band,
        "plateau": {
            "from": plateau[0]["window"],
            "to": plateau[-1]["window"],
            "width": len(plateau),
            "center": chosen["window"],
            "top_score": round(best_score, 4),
            "threshold": round(thresh, 4),
        },
        "curve": [
            {"window": c["window"], "score": c["score"],
             "flat": c["parts"]["flat"], "touch": c["parts"]["touch"]}
            for c in curve
        ],
        "rhythm": rhythm,
        "analysis": result,
        "fixed_best": fixed_best,
        "gain_vs_fixed": gain,
        "reason": (
            (band.get("note", "") + "；" if band.get("applied") else "")
            + f"在该范围内最高分 {best_score:.3f}，取高分平台 "
            + f"{plateau[0]['window']}~{plateau[-1]['window']} 天（宽 {len(plateau)} 档）"
            + f"的中心，即 {chosen['window']} 天"
        ),
    }
