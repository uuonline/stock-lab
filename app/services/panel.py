"""个股分析面板：综合评分、技术位推算、基本面解读、回测摘要。

与 ai.py 的分工：
  ai.py            负责「取数 + 调模型 + 兜底」
  本模块            负责「把数算成结论」

设计原则：
  1. 每个分数都可解释 —— 面板里给出评分明细，不是一个黑箱数字
  2. 缺失数据一律显示「—」并说明原因，绝不编造
  3. 交易位由 ATR / 布林带 / 均线机械推算，明确标注为技术位而非买卖建议
"""
from __future__ import annotations

import logging
from typing import Any

from ..services import indicators as ta

log = logging.getLogger("stocklab.panel")

# 结论档位（0~10 分）
#
# 这些阈值**不是拍脑袋定的**，而是在真实市场上标定出来的：
# 对全市场分层抽样 149 只股票，计算综合评分分布得到
#   min=3.42  p15=4.04  p35=4.38  中位=4.62  p65=4.93  p85=5.56  max=7.23
# 早期的阈值（8.5/7.0/4.5）在实践中几乎把所有股票都判成「中性」——
# 因为各项取平均后天然向中间收敛，8.5 分实际上永远达不到。
# 现在按分位划分，使「看多」= 全市场前 15%，结论才有区分度。
#
# 市场整体水位会漂移，可用 scripts/calibrate.sh 重新标定。
VERDICT_BANDS = (
    (5.56, "看多"),        # 前 15%
    (4.93, "谨慎看多"),    # 前 35%
    (4.38, "中性"),        # 中间 30%
    (4.04, "谨慎看空"),    # 后 35%
    (0.00, "看空"),        # 后 15%
)

# 标定日期与样本量，写进报告便于追溯
CALIBRATION_NOTE = "档位基于全市场 149 只分层抽样标定（2026-09-18）"


def verdict_of(score: float) -> str:
    for threshold, label in VERDICT_BANDS:
        if score >= threshold:
            return label
    return "看空"


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 综合评分

def composite_score(ctx: dict[str, Any]) -> dict[str, Any]:
    """0~10 综合评分，含可解释的明细。

    权重：技术面 50% + 基本面 35% + 估值 15%。
    港股/ETF/指数没有财报，此时自动把基本面权重并入技术面，避免因缺数据被判低分。
    """
    snap = ctx.get("tech") or {}
    v = snap.get("values") or {}
    q = ctx.get("quote") or {}
    latest = ((ctx.get("fundamentals") or {}).get("latest")) or {}

    price = _num(q.get("price"))

    # ---------- 技术面 0~10 ----------
    tech_items: list[tuple[str, float, float]] = []   # (说明, 得分, 权重)

    def add(label: str, score: float, weight: float = 1.0) -> None:
        tech_items.append((label, _clamp(score), weight))

    ma5, ma10, ma20, ma60 = (_num(v.get(k)) for k in ("ma5", "ma10", "ma20", "ma60"))
    if None not in (ma5, ma10, ma20):
        if ma5 > ma10 > ma20:
            add("均线多头排列", 9.0, 1.5)
        elif ma5 < ma10 < ma20:
            add("均线空头排列", 1.5, 1.5)
        else:
            add("均线交织", 5.0, 1.5)
    if price is not None and ma20 is not None:
        add("站上MA20" if price > ma20 else "跌破MA20", 7.5 if price > ma20 else 2.5, 1.0)
    if price is not None and ma60 is not None:
        add("站上MA60" if price > ma60 else "跌破MA60", 7.0 if price > ma60 else 3.0, 0.8)

    dif, dea = _num(v.get("dif")), _num(v.get("dea"))
    if None not in (dif, dea):
        if dif > dea and dif > 0:
            add("MACD 零轴上方金叉", 9.0, 1.2)
        elif dif > dea:
            add("MACD 金叉（零轴下）", 6.5, 1.2)
        elif dif < dea and dif < 0:
            add("MACD 零轴下方死叉", 2.0, 1.2)
        else:
            add("MACD 死叉（零轴上）", 4.0, 1.2)

    r6, j = _num(v.get("rsi6")), _num(v.get("j"))
    if r6 is not None:
        # 档位与 ai.py 的「技术面分析」保持一致的措辞，
        # 否则同一份报告里 RSI 一处写"超卖区域"、一处写"中性"，自相矛盾
        if r6 < 20:
            add(f"RSI6 严重超卖({r6:.1f})", 8.5, 0.8)
        elif r6 < 30:
            add(f"RSI6 超卖区域({r6:.1f})", 7.5, 0.8)
        elif r6 <= 45:
            add(f"RSI6 偏弱({r6:.1f})", 5.5, 0.8)
        elif r6 <= 55:
            add(f"RSI6 中性({r6:.1f})", 5.0, 0.8)
        elif r6 <= 70:
            add(f"RSI6 中性偏强({r6:.1f})", 7.0, 0.8)
        elif r6 <= 80:
            add(f"RSI6 偏高({r6:.1f})", 4.0, 0.8)
        else:
            add(f"RSI6 超买({r6:.1f})", 2.5, 0.8)
    if j is not None:
        if j > 100:
            add(f"KDJ 超买(J={j:.0f})", 3.0, 0.8)
        elif j < 0:
            add(f"KDJ 超卖(J={j:.0f})", 7.5, 0.8)
        else:
            add(f"KDJ J={j:.0f}", 5.5, 0.8)

    up, low = _num(v.get("boll_upper")), _num(v.get("boll_lower"))
    if price is not None and None not in (up, low) and up > low:
        pos = (price - low) / (up - low)      # 0=下轨 1=上轨
        # 靠近上轨短期风险大，靠近下轨相对安全
        add(f"布林位置 {pos * 100:.0f}%", _clamp(9.0 - pos * 7.0), 0.9)

    vr = _num(snap.get("vol_ratio_5"))
    if vr is not None:
        add(f"5日量比 {vr:.2f}", 6.5 if vr >= 1.2 else (4.0 if vr >= 0.8 else 3.0), 0.6)

    tech_score = 5.0
    if tech_items:
        tw = sum(w for _, _, w in tech_items)
        tech_score = sum(s * w for _, s, w in tech_items) / tw if tw else 5.0

    # ---------- 基本面 0~10 ----------
    fund_items: list[tuple[str, float]] = []

    def addf(label: str, score: float) -> None:
        fund_items.append((label, _clamp(score)))

    roe = _num(latest.get("roe"))
    if roe is not None:
        if roe >= 20:
            addf(f"ROE {roe:.1f}% 优秀", 9.0)
        elif roe >= 12:
            addf(f"ROE {roe:.1f}% 良好", 7.0)
        elif roe >= 6:
            addf(f"ROE {roe:.1f}% 一般", 5.0)
        else:
            addf(f"ROE {roe:.1f}% 偏低", 3.0)

    gm = _num(latest.get("gross_margin"))
    if gm is not None:
        addf(f"毛利率 {gm:.1f}%", 8.5 if gm >= 40 else (6.5 if gm >= 20 else 4.0))

    yoy_p = _num(latest.get("profit_yoy"))
    if yoy_p is not None:
        if yoy_p >= 20:
            addf(f"净利同比 +{yoy_p:.1f}%", 9.0)
        elif yoy_p >= 0:
            addf(f"净利同比 {yoy_p:+.1f}%", 6.5)
        elif yoy_p >= -20:
            addf(f"净利同比 {yoy_p:+.1f}%", 4.0)
        else:
            addf(f"净利同比 {yoy_p:+.1f}% 大幅下滑", 2.0)

    yoy_r = _num(latest.get("revenue_yoy"))
    if yoy_r is not None:
        if yoy_r >= 15:
            addf(f"营收同比 +{yoy_r:.1f}%", 8.5)
        elif yoy_r >= 0:
            addf(f"营收同比 {yoy_r:+.1f}%", 6.5)
        else:
            addf(f"营收同比 {yoy_r:+.1f}%", 3.5)

    debt = _num(latest.get("debt_ratio"))
    if debt is not None:
        addf(f"资产负债率 {debt:.1f}%", 8.5 if debt <= 40 else (6.5 if debt <= 60 else (4.0 if debt <= 75 else 2.5)))

    fund_score = None
    if fund_items:
        fund_score = sum(s for _, s in fund_items) / len(fund_items)

    # ---------- 估值 0~10（分越高越便宜）----------
    val_items: list[tuple[str, float]] = []
    pe = _num(q.get("pe_ttm")) or _num(q.get("pe"))
    pb = _num(q.get("pb"))
    if pe is not None:
        if pe <= 0:
            val_items.append((f"PE {pe:.1f}（亏损）", 3.0))
        elif pe <= 12:
            val_items.append((f"PE {pe:.1f} 低估区间", 9.0))
        elif pe <= 20:
            val_items.append((f"PE {pe:.1f} 合理", 7.0))
        elif pe <= 35:
            val_items.append((f"PE {pe:.1f} 偏高", 4.5))
        else:
            val_items.append((f"PE {pe:.1f} 显著偏高", 2.5))
    if pb is not None:
        if pb <= 1:
            val_items.append((f"PB {pb:.2f} 破净附近", 9.0))
        elif pb <= 3:
            val_items.append((f"PB {pb:.2f} 合理", 6.5))
        elif pb <= 6:
            val_items.append((f"PB {pb:.2f} 偏高", 4.5))
        else:
            val_items.append((f"PB {pb:.2f} 很高", 3.0))

    val_score = None
    if val_items:
        val_score = sum(s for _, s in val_items) / len(val_items)

    # ---------- 加权（缺数据的部分权重并给技术面）----------
    # 权重必须与「评分构成」里展示的项一致 —— 否则会出现
    # 「显示了估值 9.0，但权重写着仅技术面」这种自相矛盾（实际踩过）。
    if fund_score is not None and val_score is not None:
        total = tech_score * 0.50 + fund_score * 0.35 + val_score * 0.15
        weights = "技术50% / 基本面35% / 估值15%"
    elif fund_score is not None:
        total = tech_score * 0.60 + fund_score * 0.40
        weights = "技术60% / 基本面40%（该标的无估值数据）"
    elif val_score is not None:
        total = tech_score * 0.65 + val_score * 0.35
        weights = "技术65% / 估值35%（该标的无财报数据，如 ETF/指数/部分港股）"
    else:
        total = tech_score
        weights = "仅技术面（该标的既无财报也无估值数据）"

    total = round(_clamp(total), 2)
    return {
        "score": total,
        "verdict": verdict_of(total),
        "weights": weights,
        "tech_score": round(tech_score, 2),
        "fund_score": round(fund_score, 2) if fund_score is not None else None,
        "val_score": round(val_score, 2) if val_score is not None else None,
        "tech_items": tech_items,
        "fund_items": fund_items,
        "val_items": val_items,
    }


# ---------------------------------------------------------------- 技术位推算

def trading_plan(bars: list[dict], ctx: dict[str, Any]) -> dict[str, Any] | None:
    """由 ATR / 布林带 / 均线机械推算关键价位。

    ⚠️ 这些是**技术位**，不是买卖建议。它们完全由公式算出，
       不包含任何对后市的判断。真实交易还需考虑仓位、税费、流动性等。
    """
    if not bars or len(bars) < 30:
        return None
    snap = ctx.get("tech") or {}
    v = snap.get("values") or {}
    q = ctx.get("quote") or {}
    price = _num(q.get("price")) or _num(bars[-1].get("close"))
    if price is None:
        return None

    atr = _num(v.get("atr14"))
    ma20, ma60 = _num(v.get("ma20")), _num(v.get("ma60"))
    up, mid, low = _num(v.get("boll_upper")), _num(v.get("boll_mid")), _num(v.get("boll_lower"))

    closes = [b.get("close") for b in bars if b.get("close")]
    highs = [b.get("high") for b in bars if b.get("high")]
    lows = [b.get("low") for b in bars if b.get("low")]
    recent = lambda seq, n: seq[-n:] if len(seq) >= n else seq

    hi60 = max(recent(highs, 60)) if highs else None
    lo60 = min(recent(lows, 60)) if lows else None
    hi20 = max(recent(highs, 20)) if highs else None
    lo20 = min(recent(lows, 20)) if lows else None

    # 入场区间：优先用「下方最近的支撑」到「现价」之间
    cands = [x for x in (ma20, mid, lo20) if x is not None and x < price]
    if cands:
        entry_low = max(cands)             # 最近支撑
        entry_high = min(price, entry_low * 1.02)
    else:
        entry_low = price * 0.98
        entry_high = price

    # 止损：更强的支撑位，或入场价下方 2 倍 ATR，取更保守（更低）的一个
    stop_cands = [x for x in (low, ma60, lo60) if x is not None and x < entry_low]
    stop = max(stop_cands) if stop_cands else entry_low - (atr or entry_low * 0.05) * 2
    if atr:
        stop = min(stop, entry_low - atr * 1.5)
    if stop >= entry_low:
        stop = entry_low * 0.95

    # 目标位：上方压力
    t1_cands = [x for x in (up, hi20, hi60) if x is not None and x > entry_high]
    t1 = min(t1_cands) if t1_cands else entry_high * 1.08
    t2_cands = [x for x in (hi60, up) if x is not None and x > t1]
    t2 = max(t2_cands) if t2_cands else t1 * 1.05

    entry_mid = (entry_low + entry_high) / 2
    risk = entry_mid - stop
    reward1 = t1 - entry_mid
    reward2 = t2 - entry_mid
    rr1 = (reward1 / risk) if risk > 0 else None
    rr2 = (reward2 / risk) if risk > 0 else None

    # 建议仓位：按「单笔风险不超过总资金 1%」反推
    position = None
    if risk > 0 and entry_mid > 0:
        risk_pct = risk / entry_mid
        position = min(0.30, 0.01 / risk_pct) if risk_pct > 0 else None

    # 小数位按价格量级决定：4 元的 ETF 需要 3 位，1200 元的股票 2 位就够
    nd = 3 if price < 10 else 2
    return {
        "digits": nd,
        "price": round(price, nd),
        "entry_low": round(entry_low, nd),
        "entry_high": round(entry_high, nd),
        "stop": round(stop, nd),
        "target1": round(t1, nd),
        "target2": round(t2, nd),
        "risk_pct": round(risk / entry_mid * 100, 2) if entry_mid else None,
        "rr1": round(rr1, 2) if rr1 else None,
        "rr2": round(rr2, 2) if rr2 else None,
        "position_pct": round(position * 100) if position else None,
        "atr": round(atr, 3) if atr else None,
    }


# ---------------------------------------------------------------- 基本面解读

def fundamental_table(ctx: dict[str, Any]) -> list[tuple[str, str, str]]:
    """返回 (指标, 数值, 解读) 三元组。无数据时给出明确说明。"""
    q = ctx.get("quote") or {}
    latest = ((ctx.get("fundamentals") or {}).get("latest")) or {}
    if not latest:
        pe = _num(q.get("pe_ttm")) or _num(q.get("pe"))
        pb = _num(q.get("pb"))
        rows = []
        if pe is not None:
            rows.append(("市盈率(TTM)", f"{pe:.2f}", _pe_words(pe)))
        if pb is not None:
            rows.append(("市净率", f"{pb:.2f}", _pb_words(pb)))
        rows.append(("财务数据", "—", "该标的无财报披露（ETF / 指数 / 部分港股）"))
        return rows

    rows: list[tuple[str, str, str]] = []

    def add(label: str, value: Any, words: str, fmt: str = "{:.2f}%") -> None:
        if value is None:
            rows.append((label, "—", "数据缺失"))
        else:
            rows.append((label, fmt.format(value), words))

    yoy_r = _num(latest.get("revenue_yoy"))
    add("营收同比", yoy_r,
        "增长稳健" if yoy_r is not None and yoy_r >= 10 else
        ("小幅增长" if yoy_r is not None and yoy_r >= 0 else
         ("小幅下滑" if yoy_r is not None and yoy_r >= -10 else "下滑明显")))
    yoy_p = _num(latest.get("profit_yoy"))
    add("净利润同比", yoy_p,
        "利润增速快" if yoy_p is not None and yoy_p >= 15 else
        ("利润稳增" if yoy_p is not None and yoy_p >= 0 else
         ("利润承压" if yoy_p is not None and yoy_p >= -15 else "利润大幅下滑")))
    gm = _num(latest.get("gross_margin"))
    add("毛利率", gm,
        "产品盈利能力强" if gm is not None and gm >= 40 else
        ("毛利率中等" if gm is not None and gm >= 20 else "毛利率偏低"))
    roe = _num(latest.get("roe"))
    add("ROE", roe,
        "盈利能力优秀" if roe is not None and roe >= 15 else
        ("盈利能力良好" if roe is not None and roe >= 8 else "盈利能力偏弱"))
    nm = _num(latest.get("net_margin"))
    add("净利率", nm,
        "费用控制好" if nm is not None and nm >= 15 else
        ("净利率中等" if nm is not None and nm >= 5 else "净利率偏薄"))
    debt = _num(latest.get("debt_ratio"))
    add("资产负债率", debt,
        "负债低，财务稳健" if debt is not None and debt <= 40 else
        ("负债适中" if debt is not None and debt <= 65 else "负债偏高，需留意偿债压力"))

    pe = _num(q.get("pe_ttm")) or _num(q.get("pe"))
    if pe is not None:
        rows.append(("市盈率(TTM)", f"{pe:.2f}", _pe_words(pe)))
    pb = _num(q.get("pb"))
    if pb is not None:
        rows.append(("市净率", f"{pb:.2f}", _pb_words(pb)))
    eps = _num(latest.get("eps"))
    bps = _num(latest.get("bps"))
    if eps is not None and bps:
        rows.append(("每股收益/净资产", f"{eps:.2f} / {bps:.2f}", f"净资产收益率约 {eps / bps * 100:.1f}%"))
    return rows


def _pe_words(pe: float) -> str:
    if pe <= 0:
        return "公司亏损，PE 无参考意义"
    if pe <= 12:
        return "估值偏低"
    if pe <= 20:
        return "估值合理"
    if pe <= 35:
        return "估值偏高"
    return "估值显著偏高，需要业绩兑现支撑"


def _pb_words(pb: float) -> str:
    if pb <= 1:
        return "接近或低于净资产"
    if pb <= 3:
        return "估值合理"
    if pb <= 6:
        return "估值偏高"
    return "估值很高"


def growth_drivers(ctx: dict[str, Any]) -> list[str]:
    """从财务趋势推导增长驱动。

    注意：这里只能看出「财务上发生了什么」，看不出「为什么」。
    真正的增长驱动（新品、政策、订单）需要消息面数据，系统没有接入，
    所以措辞上明确写成"财务层面"，不臆造原因。
    """
    hist = ((ctx.get("fundamentals") or {}).get("history")) or []
    latest = ((ctx.get("fundamentals") or {}).get("latest")) or {}
    out: list[str] = []
    if not latest:
        return out

    yoy_r = _num(latest.get("revenue_yoy"))
    yoy_p = _num(latest.get("profit_yoy"))
    gm = _num(latest.get("gross_margin"))

    if yoy_r is not None and yoy_r > 0:
        out.append(f"营收保持增长（同比 {yoy_r:+.1f}%），业务规模仍在扩张")
    if yoy_p is not None and yoy_r is not None and yoy_p > yoy_r:
        out.append(f"利润增速（{yoy_p:+.1f}%）高于营收增速（{yoy_r:+.1f}%），盈利质量在改善")
    if gm is not None and gm >= 40:
        out.append(f"毛利率 {gm:.1f}%，产品具备较强定价能力")

    # 毛利率趋势（最近几期）
    gms = [_num(h.get("gross_margin")) for h in hist[-4:]]
    gms = [g for g in gms if g is not None]
    if len(gms) >= 3 and gms[-1] > gms[0]:
        out.append(f"毛利率近 {len(gms)} 期由 {gms[0]:.1f}% 升至 {gms[-1]:.1f}%，呈改善趋势")

    roes = [_num(h.get("roe")) for h in hist[-4:]]
    roes = [r for r in roes if r is not None]
    if len(roes) >= 3 and roes[-1] > roes[0]:
        out.append(f"ROE 近 {len(roes)} 期由 {roes[0]:.1f}% 升至 {roes[-1]:.1f}%")

    if not out:
        out.append("财务数据显示增长驱动不明显，或数据不足")
    out.append("（以上为财报数据的趋势归纳；具体驱动因素如新品、订单、政策等需结合公告判断，系统未接入消息面数据）")
    return out
