"""AI 全流程分析：6 步 13 提示词。

这套流程的思路是对的 —— 让 AI 做**信息整理和逻辑推演**，把判断留给人。
但有个前提容易被忽略：**整理用的原料必须是真实数据**。

AI 在「列出竞争对手」「解释哪些科目容易被粉饰」这类通用知识上可靠，
但在「这家公司的单一客户占比是多少」这类**具体数字**上会编 ——
因为它没有那个数据，而它被训练成倾向于给出一个像是答案的回答。
把这样的答案混进投资决策里，比没有答案更危险。

所以本模块的规则是：

    能算的，用系统里的真实数据算出来，并写明算法和数据来源；
    算不出的，明确说「本系统无此数据」，并告诉用户该去哪里找。

13 个提示词按数据可得性分三档：

  grounded  有数据，全部结论由真实数据算出（6 个）
  partial   有部分数据，能回答一部分，缺的部分明确标出（2 个）
  no_data   本系统没有这类数据，不生成任何结论（5 个）

提示词原文一字不改地保留 —— 用户可以直接复制去外部 AI 使用，
本系统负责把数据准备好、把能算的算掉。
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from ..sources import market
from . import box as box_svc
from . import indicators as ta

log = logging.getLogger("stocklab.flow")

# 财报法定披露截止对应的滞后天数。
# 关键：算历史 PE 时要用**公告可见日**而不是报告期末，
# 否则等于用还没公布的数据做决策（前视偏差），回看会虚高。
REPORT_LAG_DAYS = {"03-31": 30, "06-30": 62, "09-30": 31, "12-31": 120}

# PE 分位计算中剔除的极端值。
# 理由：利润接近零时 PE 会冲到几百上千（歌尔股份 2023 年报过一次，
# PE 到过 880），这种值不是「贵」，而是失真，混进分布会让分位失去意义。
PE_OUTLIER = 150.0

# 估值分位区间划分
PE_BANDS = (
    (20, "低估区", "当前估值低于自身历史 80% 的时间"),
    (40, "偏低区", "当前估值低于自身历史 60% 的时间"),
    (60, "合理区", "当前估值处于自身历史的中段"),
    (80, "偏高区", "当前估值高于自身历史 60% 的时间"),
    (101, "高估区", "当前估值高于自身历史 80% 的时间"),
)

# 数据来源说明
SRC_QUOTE = "腾讯/新浪行情接口（实时）"
SRC_FIN = "东财 F10 财务数据（最近 24 期报告，约 6 年）"
SRC_KLINE = "日线 K线（前复权）"


# ============================================================
# 工具
# ============================================================

def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _money(v: Any) -> str:
    """把元转成易读的亿/万元。"""
    f = _f(v)
    if f is None:
        return "—"
    a = abs(f)
    if a >= 1e8:
        return f"{f / 1e8:.2f} 亿"
    if a >= 1e4:
        return f"{f / 1e4:.2f} 万"
    return f"{f:.2f}"


def _pct_str(v: Any, digits: int = 2) -> str:
    f = _f(v)
    if f is None:
        return "—"
    return f"{f:+.{digits}f}%"


def _annualize(hist: list[dict]) -> list[dict]:
    """把季度累计数据按年归并。

    东财的 EPS / 营收是**年内累计**口径（Q1、H1、9M、年报），
    所以年度值直接取每年 12-31 那条，不能把四个季度相加。
    """
    years: dict[str, dict] = {}
    for x in hist:
        rd = str(x.get("report_date") or "")
        if not rd:
            continue
        y = rd[:4]
        if rd[5:] == "12-31":
            years[y] = {"year": y, **x}
    return [years[y] for y in sorted(years)]


# ============================================================
# 历史 PE 序列（提示词四的基础）
# ============================================================

def _ttm_eps_at(hist: list[dict], idx: int) -> float | None:
    """还原 idx 处报告期的 TTM EPS。

    东财给的是年内累计 EPS，TTM 要用标准公式还原：
        年报期        TTM = 当年累计
        其他期        TTM = 上年年报 + 本期累计 − 去年同期累计
    已用最新一期校验：算出的 PE 与行情接口给的 pe_ttm 相差约 2.5%
    （口径差异，属正常范围）。
    """
    cur = hist[idx]
    rd = str(cur.get("report_date") or "")
    if len(rd) < 10:
        return None
    mmdd, year = rd[5:], int(rd[:4])
    cur_eps = _f(cur.get("eps"))
    if cur_eps is None:
        return None
    if mmdd == "12-31":
        return cur_eps

    def find(target: str) -> float | None:
        for x in hist:
            if str(x.get("report_date") or "") == target:
                return _f(x.get("eps"))
        return None

    fy = find(f"{year - 1}-12-31")
    prev_same = find(f"{year - 1}-{mmdd}")
    if fy is None or prev_same is None:
        return None
    return fy + cur_eps - prev_same


def long_history(symbol: str, want: int = 1300) -> list[dict]:
    """取尽可能长的日线历史，用于算五年 PE 分位。

    实时接口的前复权数据只能回溯约 641~800 根（腾讯上游限制），
    不足五年。本地库 `kline_daily` 存有更长的历史（实测约 1200 根，
    回到 2021-10），可以补足。

    **但用它之前必须先确认口径一致** —— 库里是历年多次抓取的累积结果，
    如果某些日期当年是用不复权数据写进去的，拼起来会出现「接缝」，
    算出来的 PE 分位就是错的。已做的验证（002241.SZ / 600519.SH）：

      · 库与实时前复权在重叠的 800 天上，最大差异 0.04%、中位 0.000%
        → 近段口径一致；
      · 库的老段与新浪不复权差 3.9%，新段差 2.0%
        → 越久远差异越大，符合前复权序列的特征（复权累积）。
          若老段是不复权数据，它与新浪的差异应接近 0。

    结论：库里的序列口径一致，可以安全合并。合并时以实时数据为准，
    覆盖重叠日期。
    """
    from .. import db
    merged: dict[str, dict] = {}
    try:
        for b in db.load_kline(symbol):
            if b.get("date"):
                merged[str(b["date"])] = b
    except Exception as exc:  # noqa: BLE001
        log.debug("读取本地 K线失败 %s: %s", symbol, exc)

    # 库里已经有今天的收盘就意味着够新，不必再打一次实时接口 ——
    # 实时那条链路在某个源被限流时要好几秒，而这里要的只是
    # "用来算历史 PE 的长序列"，最新一根用库里的完全够。
    # 只有当库为空或明显滞后时才去补。
    today = dt.date.today().isoformat()
    db_last = max(merged) if merged else None
    need_live = (not merged) or (db_last is not None and db_last < today
                                 and _is_trading_day_gap(db_last))
    if not need_live:
        return [merged[d] for d in sorted(merged)]

    try:
        for b in market.get_kline(symbol, "day", want):
            if b.get("date"):
                merged[str(b["date"])] = b     # 实时数据覆盖同日期的库数据
    except Exception as exc:  # noqa: BLE001
        log.debug("实时 K线获取失败 %s: %s", symbol, exc)
    return [merged[d] for d in sorted(merged)]


def _is_trading_day_gap(db_last: str) -> bool:
    """库里的最后一根是否离今天太远（超过 5 个自然日就认为该补数据）。

    不能简单地用"库里不是今天"来判断：周末和节假日库里本来就不会有新数据，
    那样会导致每次请求都去补一次实时数据。
    """
    try:
        d = dt.date.fromisoformat(db_last)
    except (ValueError, TypeError):
        return True
    return (dt.date.today() - d).days > 5


def pe_history(symbol: str, fundamentals: dict | None = None,
               bars: list[dict] | None = None) -> dict[str, Any]:
    """构建历史 PE_TTM 序列，用于算百分位。

    做法：每个交易日 PE = 当日收盘价 / 当日可见的 TTM EPS。

    两个必须的处理：
      1. 用**可见日**（报告期末 + 法定披露滞后）而非报告期末，避免前视偏差；
      2. 剔除 PE > PE_OUTLIER 的失真值并如实报告剔除了多少天。
    """
    try:
        hist = sorted(
            (fundamentals or market.get_fundamentals(symbol)).get("history") or [],
            key=lambda x: str(x.get("report_date") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"财务数据获取失败: {exc}"}
    if len(hist) < 8:
        return {"ok": False, "reason": f"财务数据只有 {len(hist)} 期，不足以还原历史 PE"}

    known: list[tuple[dt.date, float]] = []
    for i, x in enumerate(hist):
        ttm = _ttm_eps_at(hist, i)
        if ttm is None or ttm <= 0:
            continue
        rd = str(x["report_date"])
        avail = dt.date.fromisoformat(rd) + dt.timedelta(
            days=REPORT_LAG_DAYS.get(rd[5:], 60)
        )
        known.append((avail, ttm))
    if not known:
        return {"ok": False, "reason": "无法还原出正向的 TTM EPS 序列"}
    known.sort()

    if bars is None:
        bars = long_history(symbol, 1300)
    if not bars:
        return {"ok": False, "reason": "无 K线数据"}

    pts: list[tuple[str, float]] = []
    skipped_noeps = 0
    for b in bars:
        try:
            bd = dt.date.fromisoformat(str(b["date"]))
        except (ValueError, TypeError):
            continue
        eps = None
        for avail, t in known:
            if avail <= bd:
                eps = t
            else:
                break
        close = _f(b.get("close"))
        if eps is None or close is None or close <= 0:
            skipped_noeps += 1
            continue
        pts.append((str(b["date"]), close / eps))

    if len(pts) < 60:
        return {"ok": False, "reason": f"可用于计算 PE 的交易日只有 {len(pts)} 天"}

    clean = [p for p in pts if p[1] <= PE_OUTLIER]
    outliers = len(pts) - len(clean)
    if len(clean) < 60:
        return {"ok": False, "reason": "清洗后样本不足"}

    vals = sorted(p[1] for p in clean)
    return {
        "ok": True,
        "points": clean,
        "values": vals,
        "days": len(clean),
        "outliers": outliers,
        "missing_eps_days": skipped_noeps,
        "start_date": clean[0][0],
        "end_date": clean[-1][0],
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
        "median": round(vals[len(vals) // 2], 2),
        "p20": round(vals[int(len(vals) * 0.2)], 2),
        "p80": round(vals[int(len(vals) * 0.8)], 2),
        "reports_used": len(hist),
    }


def pe_percentile(symbol: str, current_pe: float | None,
                  fundamentals: dict | None = None,
                  bars: list[dict] | None = None) -> dict[str, Any]:
    """当前 PE 在历史分布中的百分位。"""
    h = pe_history(symbol, fundamentals, bars)
    if not h.get("ok"):
        return {"ok": False, "reason": h.get("reason"), "history": h}
    cur = _f(current_pe)
    if cur is None:
        return {"ok": False, "reason": "缺少当前 PE", "history": h}

    vals = h["values"]
    pct = sum(1 for v in vals if v <= cur) / len(vals) * 100.0
    band, desc = "区间不明", ""
    for edge, name, d in PE_BANDS:
        if pct < edge:
            band, desc = name, d
            break

    # 用价格分位做交叉验证：PE 分位低是因为便宜，还是因为利润在涨？
    # 用同一份长历史，保证与 PE 分位同口径
    if bars is None:
        bars = long_history(symbol, 1300)
    closes = sorted(_f(b.get("close")) or 0 for b in bars)
    price_pct = None
    last_close = _f(bars[-1].get("close")) if bars else None
    if closes and last_close:
        price_pct = sum(1 for c in closes if c <= last_close) / len(closes) * 100.0

    note = None
    if price_pct is not None:
        gap = pct - price_pct
        if gap < -15:
            note = ("估值分位明显低于价格分位：股价位置不算低，但盈利增长更快，"
                    "把 PE 压下来了。这种「越涨越便宜」要看增长能不能持续。")
        elif gap > 15:
            note = ("估值分位明显高于价格分位：股价位置不高，但盈利下滑得更快，"
                    "把 PE 顶上去了。这种情况要警惕盈利继续恶化。")

    return {
        "ok": True,
        "current_pe": round(cur, 2),
        "percentile": round(pct, 1),
        "band": band,
        "band_desc": desc,
        "price_percentile": round(price_pct, 1) if price_pct is not None else None,
        "cross_note": note,
        "history": h,
    }


# ============================================================
# 支撑位 / 压力位（提示词十一）
# ============================================================

def support_resistance(bars: list[dict], price: float | None = None,
                       lookback: int = 250) -> dict[str, Any] | None:
    """从波段转折点聚类找支撑位与压力位。

    做法：zigzag 找出波段转折点 → 把价位相近的转折点聚成一簇 →
    按「触碰次数 × 时间衰减 × 成交量权重」排序 →
    取现价下方的两个作为支撑、上方的两个作为压力。

    为什么要聚类而不是直接取最高最低：单根长上影/长下影只是一个点，
    而支撑压力位的意义在于**同一个价位被反复验证过**。
    """
    if not bars or len(bars) < 60:
        return None
    seg = bars[-lookback:] if len(bars) > lookback else bars
    closes = [_f(b.get("close")) or 0 for b in seg]
    if not closes or not closes[-1]:
        return None
    cur = _f(price) or closes[-1]

    # 阈值要和窗口长度匹配：250 天用 6% 合适，但提示词十一要求看"最近三个月"
    # （约 60 个交易日），6% 只能找到 7 个转折点，聚类出来的价位太单薄。
    thr = 0.06 if len(seg) >= 150 else 0.035
    piv = box_svc.zigzag(closes, thr)
    if len(piv) < 4:
        return None

    n = len(seg)
    tol = cur * 0.02          # 2% 以内的转折点视为同一价位
    clusters: list[dict] = []
    for i in piv:
        p = closes[i]
        if not p:
            continue
        vol = _f(seg[i].get("volume")) or 0
        hit = None
        for c in clusters:
            if abs(c["price"] - p) <= tol:
                hit = c
                break
        if hit is None:
            clusters.append({"price": p, "hits": [], "sum_p": p, "vol": vol})
        else:
            hit["hits"].append(i)
            hit["sum_p"] += p
            hit["vol"] += vol

    for c in clusters:
        c["count"] = len(c["hits"]) + 1
        c["level"] = c["sum_p"] / c["count"]
        # 时间衰减：越近的验证越有意义（半衰期约 120 个交易日）
        recent = max(c["hits"] + [0])
        c["recency"] = 0.5 ** ((n - 1 - recent) / 120.0)
        c["score"] = c["count"] * (0.5 + c["recency"])

    ups = sorted([c for c in clusters if c["level"] > cur * 1.005],
                 key=lambda c: (c["level"] - cur))
    downs = sorted([c for c in clusters if c["level"] < cur * 0.995],
                   key=lambda c: (cur - c["level"]))

    # 压力位：先取最近的，同距离时取验证更多的
    def rank_near(items: list[dict]) -> list[dict]:
        out = sorted(items, key=lambda c: abs(c["level"] - cur))[:4]
        return sorted(out, key=lambda c: -c["score"])[:2]

    res = [{"price": round(c["level"], 2), "touches": c["count"],
            "score": round(c["score"], 2), "basis": "波段转折点聚集",
            "last_date": seg[max(c["hits"] + [0])].get("date")}
           for c in sorted(rank_near(ups), key=lambda c: c["level"])]
    sup = [{"price": round(c["level"], 2), "touches": c["count"],
            "score": round(c["score"], 2), "basis": "波段转折点聚集",
            "last_date": seg[max(c["hits"] + [0])].get("date")}
           for c in sorted(rank_near(downs), key=lambda c: c["level"])]

    # 提示词明确要求"两个支撑位和两个压力位"。但价格刚创新高时，
    # 上方**确实没有**历史转折点 —— 这不是数据缺失，是真实情况。
    # 此时用有明确公式的技术位兜底，并标明口径与聚集位区分开，
    # 免得用户以为"上方没有压力"或把布林上轨当成历史压力。
    snap = ta.latest_snapshot(seg) or {}
    v = snap.get("values") or {}
    highs = [_f(b.get("high")) or 0 for b in seg]
    lows = [_f(b.get("low")) or 0 for b in seg]

    def fallback_res() -> list[dict]:
        out = []
        hh = max(highs) if highs else None
        if hh and hh > cur * 1.005:
            out.append({"price": round(hh, 2), "touches": 0, "score": 0,
                        "basis": f"近 {n} 日最高价", "last_date": seg[-1].get("date")})
        up = _f(v.get("upper")) or _f(v.get("boll_up"))
        if up and up > cur * 1.005:
            out.append({"price": round(up, 2), "touches": 0, "score": 0,
                        "basis": "布林上轨", "last_date": seg[-1].get("date")})
        return out

    def fallback_sup() -> list[dict]:
        out = []
        ll = min(lows) if lows else None
        if ll and ll < cur * 0.995:
            out.append({"price": round(ll, 2), "touches": 0, "score": 0,
                        "basis": f"近 {n} 日最低价", "last_date": seg[-1].get("date")})
        lo = _f(v.get("lower")) or _f(v.get("boll_low"))
        if lo and lo < cur * 0.995:
            out.append({"price": round(lo, 2), "touches": 0, "score": 0,
                        "basis": "布林下轨", "last_date": seg[-1].get("date")})
        return out

    res_note = sup_note = None
    if len(res) < 2:
        have = {x["price"] for x in res}
        for x in fallback_res():
            if len(res) >= 2:
                break
            if x["price"] not in have:
                res.append(x)
                have.add(x["price"])
        if any(x["basis"] != "波段转折点聚集" for x in res):
            res_note = ("上方缺少被反复验证过的历史压力位"
                        "（价格已在近期高位，上方没有成交密集区），"
                        "不足的两个用技术位补齐，口径已分别标注。")
    if len(sup) < 2:
        have = {x["price"] for x in sup}
        for x in fallback_sup():
            if len(sup) >= 2:
                break
            if x["price"] not in have:
                sup.append(x)
                have.add(x["price"])
        if any(x["basis"] != "波段转折点聚集" for x in sup):
            sup_note = "下方被验证过的支撑不足两个，其余用技术位补齐。"

    return {
        "price": round(cur, 2),
        "supports": sorted(sup, key=lambda x: -x["price"]),
        "resistances": sorted(res, key=lambda x: x["price"]),
        "pivots": len(piv),
        "clusters": len(clusters),
        "lookback": n,
        "zigzag_thr": round(thr * 100, 1),
        "res_note": res_note,
        "sup_note": sup_note,
    }


# ============================================================
# 情景推演（提示词十二）
# ============================================================

def scenarios(bars: list[dict], price: float | None = None,
              box: dict | None = None) -> dict[str, Any] | None:
    """推演未来三个月的三种情况。

    ⚠️ 这里算的是**波动率的统计区间**，不是对后市的预测。
       做法：用近 60 日对数收益的标准差推到 3 个月（约 63 个交易日），
       再和真实的关键价位（均线、箱体上下沿、支撑压力位）对齐。
       概率来自正态假设 —— 真实市场是厚尾的，所以概率只作参考量级。
    """
    if not bars or len(bars) < 60:
        return None
    closes = [_f(b.get("close")) or 0 for b in bars]
    cur = _f(price) or closes[-1]
    if not cur:
        return None

    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 30:
        return None
    recent = rets[-60:]
    mean = sum(recent) / len(recent)
    var = sum((r - mean) ** 2 for r in recent) / max(1, len(recent) - 1)
    sd = math.sqrt(var)

    H = 63                                   # 未来约 3 个月的交易日数
    sig = sd * math.sqrt(H)
    drift = mean * H

    # 正态假设下的概率
    def prob_above(x: float) -> float:
        if sig <= 0:
            return 0.0
        z = (math.log(x / cur) - drift) / sig
        return 0.5 * (1 - math.erf(z / math.sqrt(2)))

    up_1 = cur * math.exp(drift + sig)
    up_2 = cur * math.exp(drift + 2 * sig)
    dn_1 = cur * math.exp(drift - sig)
    dn_2 = cur * math.exp(drift - 2 * sig)

    # 关键价位：优先用真实结构，没有就用统计值
    snap = ta.latest_snapshot(bars) or {}
    v = snap.get("values") or {}
    ma60 = _f(v.get("ma60")) or _f(v.get("ma20"))
    sr = support_resistance(bars, cur) or {}
    res = (sr.get("resistances") or [])
    sup = (sr.get("supports") or [])
    box_top = _f((box or {}).get("top"))
    box_bottom = _f((box or {}).get("bottom"))

    opt_target = res[0]["price"] if res else round(up_1, 2)
    mid_low = sup[0]["price"] if sup else round(dn_1, 2)
    mid_high = res[0]["price"] if res else round(up_1, 2)
    pess_target = (box_bottom if box_bottom and box_bottom < (sup[0]["price"] if sup else 1e18)
                   else (sup[0]["price"] if sup else round(dn_2, 2)))

    def pct_of(target: float) -> str:
        return f"{(target / cur - 1) * 100:+.1f}%"

    return {
        "price": round(cur, 2),
        "daily_vol_pct": round(sd * 100, 2),
        "horizon_days": H,
        "sigma_3m_pct": round(sig * 100, 1),
        "stats": {
            "up_1sigma": round(up_1, 2), "up_2sigma": round(up_2, 2),
            "down_1sigma": round(dn_1, 2), "down_2sigma": round(dn_2, 2),
        },
        "levels": {"ma60": ma60, "box_top": box_top, "box_bottom": box_bottom,
                   "resistances": [r["price"] for r in res],
                   "supports": [s["price"] for s in sup]},
        "cases": [
            {
                "name": "乐观",
                "prob": round(prob_above(opt_target) * 100, 1),
                "target": opt_target,
                "change": pct_of(opt_target),
                "desc": (f"站上 {opt_target}（{pct_of(opt_target)}）。"
                         f"{'这是最近的一个真实压力位，' if res else ''}"
                         f"若能放量站稳，上看 {round(up_2, 2)} 附近。"),
                "plan": (f"突破 {opt_target} 且成交量不低于近 5 日均量的 1.2 倍，"
                         f"可考虑持有；若冲高后回落跌回该价位下方，"
                         f"视为假突破，减回原仓位。"),
                "trigger": f"收盘站上 {opt_target} 且放量",
            },
            {
                "name": "震荡",
                "prob": round((prob_above(mid_low) - prob_above(mid_high)) * 100, 1),
                "target": f"{mid_low} ~ {mid_high}",
                "change": f"{pct_of(mid_low)} ~ {pct_of(mid_high)}",
                "desc": (f"在 {mid_low} ~ {mid_high} 之间反复。"
                         f"这是波动率中性假设下最可能的情形："
                         f"近 60 日日均波动 {sd * 100:.2f}%，三个月累计一个标准差是 "
                         f"{sig * 100:.1f}%。"),
                "plan": ("区间内不追涨杀跌；接近上沿减一部分、接近下沿再考虑补回。"
                         "没有明确方向的震荡里，频繁交易的手续费和滑点往往吃掉全部收益。"),
                "trigger": f"始终未能有效突破 {mid_high}",
            },
            {
                "name": "悲观",
                "prob": round((1 - prob_above(pess_target)) * 100, 1),
                "target": pess_target,
                "change": pct_of(pess_target),
                "desc": (f"回落到 {pess_target}（{pct_of(pess_target)}）。"
                         f"{'这是箱体下沿，' if box_bottom and abs(pess_target - box_bottom) < 0.01 else ''}"
                         f"跌破后下方看 {round(dn_2, 2)} 附近的统计下界。"),
                "plan": (f"跌破 {pess_target} 且不能当日收回，"
                         f"说明原有区间失效，应考虑降低仓位而不是补仓摊薄成本"
                         f"（「越跌越买」在趋势下行里是亏得最快的方式）。"),
                "trigger": f"放量跌破 {pess_target}",
            },
        ],
        "caveat": ("三种情况的概率来自正态分布假设，真实市场存在厚尾，"
                   "极端行情比正态预测的更频繁 —— 概率只用于比较量级，不要当精确值。"),
    }


# ============================================================
# 13 个提示词
# ============================================================

PROMPTS: list[dict[str, Any]] = [
    # ---- 第一步 ----
    {"id": "p1", "step": 1, "title": "商业模式与收入来源",
     "prompt": "请用一句话概括这家公司的核心商业模式并列出它最主要的收入来源是什么。",
     "status": "no_data"},
    {"id": "p2", "step": 1, "title": "行业竞争对手",
     "prompt": "请列出这家公司在行业中的前三大竞争对手，并说明每家公司的核心优势分别是什么。",
     "status": "no_data"},
    {"id": "p3", "step": 1, "title": "近三年营收与净利趋势",
     "prompt": "请分析这家公司近三年的营收和净利润变化趋势，按年份列出具体数字并计算每年的增速。",
     "status": "grounded"},
    # ---- 第二步 ----
    {"id": "p4", "step": 2, "title": "市盈率历史百分位",
     "prompt": ("请用历史百分位法计算这只股票当前市盈率在过去五年中的位置，"
                "告诉我它处于高估区、低估区还是合理区间。"),
     "status": "grounded"},
    {"id": "p5", "step": 2, "title": "市净率与净资产收益率对比",
     "prompt": ("请将这支股票的市净率和净资产收益率与同行业另外两家龙头公司进行对比"
                "并给出简单的结论。"),
     "status": "partial"},
    {"id": "p6", "step": 2, "title": "估值与成长性是否匹配",
     "prompt": ("请结合公司最近三年的营收增速判断当前估值水平是否匹配他的成长性，"
                "如果匹配就说匹配，不匹配就说不匹配。"),
     "status": "grounded"},
    # ---- 第三步 ----
    {"id": "p7", "step": 3, "title": "最容易被粉饰的科目",
     "prompt": ("请列出这只股票在财务报表中最容易被粉饰的三个科目并解释为什么这些科目"
                "容易出问题。"),
     "status": "partial"},
    {"id": "p8", "step": 3, "title": "客户与供应商集中度",
     "prompt": ("请分析这家公司是否存在单一客户依赖或单一供应商依赖，"
                "如果有的话分别占比是多少。"),
     "status": "no_data"},
    {"id": "p9", "step": 3, "title": "大股东与高管增减持",
     "prompt": ("请分析这只股票过去一年内大股东和高管的增减持情况，"
                "并告诉我整体是净买入还是净卖出。"),
     "status": "no_data"},
    # ---- 第四步 ----
    {"id": "p10", "step": 4, "title": "均线位置与通道",
     "prompt": ("请用均线系统分析这只股票当前股价位于五日线、20 日线和 60 日线的什么位置，"
                "并告诉我目前处于上升通道还是下降通道。"),
     "status": "grounded"},
    {"id": "p11", "step": 4, "title": "支撑位与压力位",
     "prompt": "请根据最近三个月的 K 线走势找出股价最重要的两个支撑位和两个压力位。",
     "status": "grounded"},
    # ---- 第五步 ----
    {"id": "p12", "step": 5, "title": "未来三个月三种情景",
     "prompt": ("假设我现在买入这只股票，请帮我推演未来三个月可能出现的三种独立情况。"
                "每种情况都要具体描述并为每种情况给出一个简单的应对方案。"),
     "status": "grounded"},
    # ---- 第六步 ----
    {"id": "p13", "step": 6, "title": "综合操作建议",
     "prompt": ("请基于以上所有信息用不超过 100 字给出这只股票当前的操作建议，"
                "包括买入、持有还是卖出以及对应的仓位建议。"),
     "status": "grounded"},
]

STEP_TITLES = {
    1: "第一步 · 快速了解这家公司到底在做什么",
    2: "第二步 · 搞清楚这家公司到底值多少钱",
    3: "第三步 · 排查有没有隐藏的风险",
    4: "第四步 · 分析技术面的买卖时机",
    5: "第五步 · 推演最坏的情况",
    6: "第六步 · 把前面的分析串起来",
}

# 无数据提示词的建议来源
NO_DATA_HINT = {
    "p1": ("公司业务与主营构成属于公司公告信息。本系统不接这类文本数据源，"
           "因为靠抓取拼出来的业务描述往往过时或错配，不如让用户直接看原始公告。",
           ["巨潮资讯网（cninfo.com.cn）公司年报「业务概要」章节",
            "公司官网「关于我们」",
            "把公司名称直接丢给 AI 问答，这类通用信息 AI 是可靠的"]),
    "p2": ("本系统没有行业分类与同业数据库，无法判断谁是「龙头」。"
           "我们不猜 —— 猜出来的竞争对手会直接带偏整条分析链。",
           ["行情软件的「所属行业」+「行业排名」",
            "公司年报「行业竞争格局」章节",
            "问财/同花顺 iFinD 等支持自然语言选股的工具"]),
    "p8": ("客户与供应商集中度只在**年报附注**里披露，行情接口不提供，"
           "本系统没有这个数据源。",
           ["年报「前五名客户/供应商情况」附注（有具体占比）",
            "巨潮资讯网 002241 年报全文搜索「前五名客户」"]),
    "p9": ("大股东与高管增减持属于交易所公告数据，本系统不接公告源。",
           ["巨潮资讯网「股东增减持」栏目",
            "交易所官网「董监高持股变动」",
            "行情软件的「股东研究 → 高管持股变动」"]),
}


def _gap(prompt_id: str, extra: list[str] | None = None) -> dict[str, Any]:
    reason, where = NO_DATA_HINT.get(prompt_id, ("本系统无此数据。", []))
    return {"reason": reason, "where": where, "extra": extra or []}


# ============================================================
# 各提示词的数据装配
# ============================================================

def _ans_p3(ctx: dict) -> dict[str, Any]:
    hist = (ctx.get("fundamentals") or {}).get("history") or []
    all_years = _annualize(hist)
    if len(all_years) < 2:
        return {"status": "partial", "data": None,
                "text": "本系统只拿到不足两年的年报数据，无法给出三年趋势。",
                "gap": _gap("p3", ["年报数据不足，可能是指数、ETF 或次新股"])}
    # 提示词问的是"近三年"。但手上现在有 6 年数据 —— 三年看趋势、
    # 更长区间看稳定性，两者都有用：只看三年容易被某一年的一次性因素
    # 误导（歌尔股份 2023 年利润 -76.8%，就属于这种）。
    years = all_years[-3:] if len(all_years) >= 3 else all_years
    long_years = all_years

    rows = []
    prev_rev = prev_np = None
    for y in years:
        rev, np_ = _f(y.get("revenue")), _f(y.get("net_profit"))
        rows.append({
            "year": y["year"],
            "revenue": rev, "revenue_text": _money(rev),
            "revenue_yoy": _f(y.get("revenue_yoy")),
            "net_profit": np_, "net_profit_text": _money(np_),
            "net_profit_yoy": _f(y.get("profit_yoy")),
            "roe": _f(y.get("roe")),
            "gross_margin": _f(y.get("gross_margin")),
            "net_margin": _f(y.get("net_margin")),
            # 年度同比：优先用数据源自带的（它是"本年报 vs 上一年报"），
            # 自己算的话第一年没有前一年可比、会少一个点，趋势判断就不完整
            "rev_yoy": _f(y.get("revenue_yoy")),
            "np_yoy": _f(y.get("profit_yoy")),
        })
        prev_rev, prev_np = rev or prev_rev, np_ or prev_np

    first, last = rows[0], rows[-1]
    yrs = len(rows) - 1
    rev_cagr = np_cagr = None
    if yrs > 0 and first["revenue"] and last["revenue"] and first["revenue"] > 0:
        rev_cagr = ((last["revenue"] / first["revenue"]) ** (1 / yrs) - 1) * 100
    if yrs > 0 and first["net_profit"] and last["net_profit"] and first["net_profit"] > 0:
        np_cagr = ((last["net_profit"] / first["net_profit"]) ** (1 / yrs) - 1) * 100

    # 判断增速方向
    trend = "数据不足"
    revs = [r["rev_yoy"] for r in rows if r["rev_yoy"] is not None]
    if len(revs) >= 3:
        if all(revs[i] < revs[i - 1] for i in range(1, len(revs))):
            trend = "营收增速逐年放缓"
        elif all(revs[i] > revs[i - 1] for i in range(1, len(revs))):
            trend = "营收增速逐年加快"
        else:
            trend = "营收增速有起伏，未形成单一方向"
    elif len(revs) == 2:
        trend = ("营收增速" + ("放缓" if revs[1] < revs[0] else "回升")
                 + "（仅两个年度可比，仅供参考）")

    # 基期效应检查：如果起始年份的利润是暴跌的，那么"三年复合增速"会被
    # 一个被人为压低的基数放大，看起来成长性很好，其实只是从坑里爬回来。
    # 歌尔股份就是典型：2023 年净利润 -76.8%，导致 2023→2025 复合增速算出来
    # 是 +90%，看着像高成长股。
    base_effect = None
    base_yoy = _f(rows[0].get("profit_yoy"))
    if base_yoy is not None and base_yoy < -25:
        base_effect = (f"注意基期效应：{rows[0]['year']} 年净利润同比 "
                       f"{base_yoy:+.1f}%（大幅下滑），以这一年的低利润为基数算出的"
                       f"复合增速会被显著放大 —— 它衡量的是「从坑里爬回来」的速度，"
                       f"不代表长期成长能力。看成长性时建议改用绝对利润额或更长区间。")

    nps = [r["np_yoy"] for r in rows if r["np_yoy"] is not None]
    np_trend = "数据不足"
    if len(nps) >= 3:
        if all(nps[i] < nps[i - 1] for i in range(1, len(nps))):
            np_trend = "净利润增速逐年放缓"
        elif all(nps[i] > nps[i - 1] for i in range(1, len(nps))):
            np_trend = "净利润增速逐年加快"
        else:
            np_trend = "净利润增速波动较大"
    elif len(nps) == 2:
        np_trend = ("净利润增速" + ("放缓" if nps[1] < nps[0] else "回升")
                    + "（仅两个年度可比，仅供参考）")

    # 更长区间的参考值
    long_rev = long_np = None
    if len(long_years) > len(years):
        ly = len(long_years) - 1
        r0, r1 = _f(long_years[0].get("revenue")), _f(long_years[-1].get("revenue"))
        n0, n1 = _f(long_years[0].get("net_profit")), _f(long_years[-1].get("net_profit"))
        if r0 and r1 and r0 > 0:
            long_rev = ((r1 / r0) ** (1 / ly) - 1) * 100
        if n0 and n1 and n0 > 0:
            long_np = ((n1 / n0) ** (1 / ly) - 1) * 100

    parts = [f"近三年（{first['year']}→{last['year']}）："]
    parts.append(f"营收 {first['revenue_text']} → {last['revenue_text']}"
                 + (f"，年均复合 {rev_cagr:+.1f}%" if rev_cagr is not None else "") + "；")
    parts.append(f"净利润 {first['net_profit_text']} → {last['net_profit_text']}"
                 + (f"，年均复合 {np_cagr:+.1f}%" if np_cagr is not None else "") + "。")
    parts.append(f"{trend}；{np_trend}。")
    if long_rev is not None or long_np is not None:
        bits = []
        if long_rev is not None:
            bits.append(f"营收 {long_rev:+.1f}%")
        if long_np is not None:
            bits.append(f"净利润 {long_np:+.1f}%")
        parts.append(f"参考更长区间（{long_years[0]['year']}→{long_years[-1]['year']}，"
                     f"共 {len(long_years)} 年）年均复合：" + "、".join(bits) + "。"
                     f"两者差距越大，说明近三年受一次性因素影响越大。")

    if base_effect:
        parts.append(base_effect)

    return {
        "status": "grounded",
        "data": {"years": rows, "rev_cagr": round(rev_cagr, 2) if rev_cagr is not None else None,
                 "np_cagr": round(np_cagr, 2) if np_cagr is not None else None,
                 "rev_trend": trend, "np_trend": np_trend,
                 "base_effect": base_effect,
                 "long_range": {"from": long_years[0]["year"], "to": long_years[-1]["year"],
                                "years": len(long_years),
                                "rev_cagr": round(long_rev, 2) if long_rev is not None else None,
                                "np_cagr": round(long_np, 2) if long_np is not None else None},
                 "all_year_labels": [y["year"] for y in long_years]},
        "text": "".join(parts),
        "gap": None,
        "source": SRC_FIN,
        "method_caveat": base_effect,
    }


def _ans_p4(ctx: dict) -> dict[str, Any]:
    sym = ctx["symbol"]
    q = ctx.get("quote") or {}
    r = pe_percentile(sym, q.get("pe_ttm"), ctx.get("fundamentals"),
                      ctx.get("long_bars"))
    if not r.get("ok"):
        return {"status": "partial", "data": None,
                "text": f"无法计算 PE 历史分位：{r.get('reason')}",
                "gap": _gap("p4", ["港股/ETF 通常没有完整的历史 PE 序列"])}

    h = r["history"]
    text = (f"当前 PE(TTM) {r['current_pe']}，落在近 {h['days']} 个交易日"
            f"（{h['start_date']} ~ {h['end_date']}）的 {r['percentile']}% 分位，"
            f"属于**{r['band']}**。区间内 PE 最低 {h['min']}、中位 {h['median']}、"
            f"最高 {h['max']}。")
    if r.get("cross_note"):
        text += r["cross_note"]
    return {
        "status": "grounded",
        "data": r,
        "text": text,
        "gap": None,
        "source": (f"{SRC_FIN} + {SRC_KLINE}。"
                   f"算法：逐日 PE = 当日收盘价 / 当日可见的 TTM EPS，"
                   f"用法定披露滞后 {REPORT_LAG_DAYS} 避免前视偏差；"
                   f"剔除 PE > {PE_OUTLIER:.0f} 的失真值 {h['outliers']} 天"
                   f"（盈利接近零时 PE 会失真，不是真的贵）。"),
        "method_caveat": (None if h["days"] >= 1000 else
                          f"样本仅 {h['days']} 个交易日，不足完整五年，分位仅供参考。"),
    }


def _ans_p5(ctx: dict) -> dict[str, Any]:
    sym = ctx["symbol"]
    q = ctx.get("quote") or {}
    latest = (ctx.get("fundamentals") or {}).get("latest") or {}
    pb, roe = _f(q.get("pb")), _f(latest.get("roe"))

    # 本系统能做的对比：与自身历史比 + 与全市场比
    own = {"pb": pb, "roe": roe, "pe": _f(q.get("pe_ttm"))}
    market_rank = None
    try:
        import sqlite3
        from ..config import settings
        con = sqlite3.connect(str(settings.db_path))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT pb FROM market_snapshot WHERE asset_type='stock' "
            "AND pb IS NOT NULL AND pb > 0 AND pb < 60"
        ).fetchall()
        con.close()
        if rows and pb:
            vals = sorted(r["pb"] for r in rows)
            below = sum(1 for v in vals if v <= pb)
            market_rank = {
                "total": len(vals),
                "percentile": round(below / len(vals) * 100, 1),
                "median": round(vals[len(vals) // 2], 2),
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("全市场 PB 分位计算失败: %s", exc)

    text = []
    if pb is not None:
        text.append(f"本股 PB {pb}")
    if roe is not None:
        text.append(f"ROE {roe}%")
    if market_rank:
        text.append(f"PB 在全市场 {market_rank['total']} 只股票中处于 "
                    f"{market_rank['percentile']}% 分位（全市场中位 {market_rank['median']}）")
    body = "，".join(text) + "。" if text else "缺少 PB / ROE 数据。"
    body += ("**同行业另外两家龙头的对比本系统做不了** —— "
             "我们没有行业分类和同业数据库，无法判断谁是龙头。"
             "硬凑两个名字出来只会污染后面的分析。")

    return {
        "status": "partial",
        "data": {"own": own, "market_rank": market_rank},
        "text": body,
        "gap": _gap("p5", ["行情软件的「行业对比」功能可直接看到同业 PB/ROE 排序"]),
        "source": f"{SRC_QUOTE} + 本地全市场快照",
    }


def _ans_p6(ctx: dict) -> dict[str, Any]:
    q = ctx.get("quote") or {}
    hist = (ctx.get("fundamentals") or {}).get("history") or []
    all_years = _annualize(hist)
    # 提示词说的是"最近三年的营收增速"，所以用近三年
    years = all_years[-3:] if len(all_years) >= 3 else all_years
    pe = _f(q.get("pe_ttm"))

    if len(years) < 2 or not pe:
        return {"status": "partial", "data": None,
                "text": "缺少足够的历史年报或 PE 数据，无法判断估值与成长性是否匹配。",
                "gap": _gap("p6", ["港股/ETF 通常没有完整财报"])}

    # 用三年净利润复合增速做 PEG 的近似
    first, last = years[0], years[-1]
    yrs = len(years) - 1
    growth = None
    if yrs > 0:
        r0, r1 = _f(first.get("net_profit")), _f(last.get("net_profit"))
        if r0 and r1 and r0 > 0:
            growth = ((r1 / r0) ** (1 / yrs) - 1) * 100
    rev_growth = None
    if yrs > 0:
        v0, v1 = _f(first.get("revenue")), _f(last.get("revenue"))
        if v0 and v1 and v0 > 0:
            rev_growth = ((v1 / v0) ** (1 / yrs) - 1) * 100

    peg = None
    if growth and growth > 0:
        peg = pe / growth

    # 基期被压低时 PEG 会失真（分母被放大），必须说明
    base_yoy = _f(first.get("profit_yoy"))
    base_note = None
    if base_yoy is not None and base_yoy < -25:
        base_note = (f"但要注意：{first['year']} 年净利润同比 {base_yoy:+.1f}%，"
                     f"基期被大幅压低，{growth:+.1f}% 的复合增速含有「恢复性增长」成分。"
                     f"用这个增速算 PEG 会偏向「匹配」，结论要打折扣。")

    # 营收比净利润稳定得多，用它做交叉验证：
    # 如果"利润增速"和"营收增速"差很远，说明利润变化主要不是经营驱动
    rev_peg = (pe / rev_growth) if (rev_growth and rev_growth > 0) else None

    if peg is None:
        verdict, why = "无法判断", "净利润复合增速为负或数据缺失，PEG 不适用。"
    elif peg < 0.8:
        verdict, why = "匹配", f"PEG≈{peg:.2f}（<0.8），估值相对成长性偏便宜。"
    elif peg <= 1.2:
        verdict, why = "匹配", f"PEG≈{peg:.2f}，估值与成长性大致相称。"
    elif peg <= 2.0:
        verdict, why = "勉强匹配", f"PEG≈{peg:.2f}，估值跑在了成长前面。"
    else:
        verdict, why = "不匹配", f"PEG≈{peg:.2f}（>2），估值明显高于成长性所能支撑的水平。"

    text = ""
    if peg:
        text = (f"近三年（{first['year']}→{last['year']}），净利润年均复合 "
                f"{growth:+.1f}%，营收年均复合 {rev_growth:+.1f}%，"
                f"当前 PE(TTM) {pe:.1f}，PEG≈{peg:.2f}。")
    text += f"结论：**{verdict}** —— {why}"
    if base_note:
        text += base_note
        if rev_peg:
            text += (f"用更稳定的营收口径交叉验证：营收年均复合 {rev_growth:+.1f}%，"
                     f"对应 PEG≈{rev_peg:.2f}，"
                     + ("这个口径下估值与成长性**不匹配**。" if rev_peg > 1.5
                        else "这个口径下两者大致**匹配**。"))

    return {
        "status": "grounded",
        "data": {"pe": pe, "profit_cagr": round(growth, 2) if growth else None,
                 "rev_cagr": round(rev_growth, 2) if rev_growth else None,
                 "peg": round(peg, 2) if peg else None,
                 "rev_peg": round(rev_peg, 2) if rev_peg else None,
                 "base_effect": base_note,
                 "range": f"{first['year']}→{last['year']}",
                 "verdict": verdict, "why": why},
        "text": text,
        "gap": None,
        "source": f"{SRC_QUOTE} + {SRC_FIN}",
        "method_caveat": ("PEG 是粗略口径：它假设未来增速等于过去增速，"
                          "而周期股、一次性收益都会让这个假设失效。"),
    }


def _ans_p7(ctx: dict) -> dict[str, Any]:
    hist = (ctx.get("fundamentals") or {}).get("history") or []
    latest = (ctx.get("fundamentals") or {}).get("latest") or {}
    years = _annualize(hist)

    # 我们能查的是这三个"侧面"：它们各自对应的可粉饰科目会露出痕迹
    checks = []
    # 1) 营收与利润增速是否背离（收入虚增的信号）
    if len(years) >= 2:
        last = years[-1]
        rv, nv = _f(last.get("revenue_yoy")), _f(last.get("profit_yoy"))
        if rv is not None and nv is not None:
            diverged = abs(rv - nv) > 15
            checks.append({
                "item": "营收与净利润增速是否背离",
                "value": f"营收 {_pct_str(rv)}，净利 {_pct_str(nv)}",
                "flag": diverged,
                "read": ("背离超过 15 个百分点，需要看是毛利率变化、"
                         "费用异常还是非经常性损益造成"
                         if diverged else "两者方向与幅度基本一致"),
            })
    # 2) 净利率趋势（费用或收入确认问题的信号）
    nms = [(y["year"], _f(y.get("net_margin"))) for y in years]
    nms = [(y, v) for y, v in nms if v is not None]
    if len(nms) >= 2:
        delta = nms[-1][1] - nms[0][1]
        checks.append({
            "item": "净利率趋势",
            "value": f"{nms[0][0]} 年 {nms[0][1]:.2f}% → {nms[-1][0]} 年 {nms[-1][1]:.2f}%",
            "flag": abs(delta) > 5,
            "read": ("净利率变动超过 5 个百分点，需要拆开看是成本、"
                     "费用还是非经常损益" if abs(delta) > 5 else "净利率相对稳定"),
        })
    # 3) 资产负债率（负债与表外风险的信号）
    drs = [(y["year"], _f(y.get("debt_ratio"))) for y in years]
    drs = [(y, v) for y, v in drs if v is not None]
    if len(drs) >= 2:
        delta = drs[-1][1] - drs[0][1]
        checks.append({
            "item": "资产负债率趋势",
            "value": f"{drs[0][0]} 年 {drs[0][1]:.2f}% → {drs[-1][0]} 年 {drs[-1][1]:.2f}%",
            "flag": delta > 8,
            "read": ("负债率上升超过 8 个百分点，扩张靠加杠杆，"
                     "要留意偿债与利息支出" if delta > 8 else "负债水平变化不大"),
        })
    # 4) 毛利率（成本转嫁能力）
    gms = [(y["year"], _f(y.get("gross_margin"))) for y in years]
    gms = [(y, v) for y, v in gms if v is not None]
    if len(gms) >= 2:
        delta = gms[-1][1] - gms[0][1]
        checks.append({
            "item": "毛利率趋势",
            "value": f"{gms[0][0]} 年 {gms[0][1]:.2f}% → {gms[-1][0]} 年 {gms[-1][1]:.2f}%",
            "flag": delta < -5,
            "read": ("毛利率下滑超过 5 个百分点，可能是价格战、"
                     "成本上升或产品结构变化" if delta < -5 else "毛利率未明显恶化"),
        })

    flagged = [c for c in checks if c["flag"]]
    text = ("财务报表里最常被粉饰的三个科目，**在通用会计层面**是固定的："
            "① **应收账款**——提前确认收入时挂在这里，收入上去了现金没回来；"
            "② **存货**——成本该结转却不结转，虚增利润；"
            "③ **商誉与减值准备**——并购时高估标的，该计提的减值推迟计提。"
            "这三者的共同点是都需要**主观判断**，所以有操作空间。"
            "但**具体到这只股票这三个科目的数字，本系统没有**（只有"
            "汇总财务指标，没有附注明细），无法判断它是否真的存在粉饰。"
            "下面是本系统能查到的侧面信号：")
    if flagged:
        text += (f"共 {len(flagged)} 项触发关注 —— "
                 + "；".join(f"{c['item']}：{c['read']}" for c in flagged))
    else:
        text += "四项侧面检查均未触发关注阈值。"

    return {
        "status": "partial",
        "data": {"checks": checks, "flagged": len(flagged),
                 "latest": {"report_name": latest.get("report_name"),
                            "gross_margin": _f(latest.get("gross_margin")),
                            "net_margin": _f(latest.get("net_margin")),
                            "debt_ratio": _f(latest.get("debt_ratio"))}},
        "text": text,
        "gap": _gap("p7", [
            "年报附注「应收账款」「存货」「商誉」三个科目的明细与账龄结构",
            "现金流量表「经营活动现金流净额」与净利润的比值（长期低于 1 要警惕）",
        ]),
        "source": SRC_FIN,
    }


def _ans_p10(ctx: dict) -> dict[str, Any]:
    bars = ctx.get("bars") or []
    q = ctx.get("quote") or {}
    snap = ctx.get("tech") or {}
    v = snap.get("values") or {}
    price = _f(q.get("price")) or (_f(bars[-1].get("close")) if bars else None)
    ma5, ma10, ma20, ma60 = (_f(v.get("ma5")), _f(v.get("ma10")),
                             _f(v.get("ma20")), _f(v.get("ma60")))
    if not price or not ma20:
        return {"status": "partial", "data": None,
                "text": "K线不足，无法计算均线位置。", "gap": _gap("p10")}

    def rel(ma: float | None) -> str:
        if not ma:
            return "—"
        d = (price / ma - 1) * 100
        return f"{'上方' if d >= 0 else '下方'} {abs(d):.2f}%"

    # 均线排列判断通道
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            channel = "多头排列（上升通道）"
        elif ma5 < ma20 < ma60:
            channel = "空头排列（下降通道）"
        else:
            channel = "均线交织（震荡，无明确通道）"
    else:
        channel = "数据不足"

    # 用 MA60 斜率交叉验证：均线排列是快照，斜率才是方向
    slope_note = ""
    closes = [_f(b.get("close")) or 0 for b in bars]
    if len(closes) >= 80:
        ma60_now = sum(closes[-60:]) / 60
        ma60_prev = sum(closes[-80:-20]) / 60
        if ma60_prev:
            sl = (ma60_now / ma60_prev - 1) * 100
            slope_note = (f"MA60 近 20 日{'上行' if sl > 0 else '下行'} "
                          f"{abs(sl):.2f}%，与均线排列"
                          f"{'一致' if (sl > 0) == (ma5 or 0) > (ma60 or 0) else '不一致（可能是刚转折）'}。")

    parts = [f"现价 {price}"]
    if ma5:
        parts.append(f"MA5 {ma5}（{rel(ma5)}）")
    if ma20:
        parts.append(f"MA20 {ma20}（{rel(ma20)}）")
    if ma60:
        parts.append(f"MA60 {ma60}（{rel(ma60)}）")
    text = "，".join(parts) + f"。均线系统呈 **{channel}**。" + slope_note

    return {
        "status": "grounded",
        "data": {"price": price, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
                 "channel": channel, "slope_note": slope_note,
                 "rel": {"ma5": rel(ma5), "ma20": rel(ma20), "ma60": rel(ma60)}},
        "text": text,
        "gap": None,
        "source": SRC_KLINE,
    }


def _ans_p11(ctx: dict) -> dict[str, Any]:
    bars = ctx.get("bars") or []
    q = ctx.get("quote") or {}
    price = _f(q.get("price")) or (_f(bars[-1].get("close")) if bars else None)
    sr = support_resistance(bars, price, lookback=60)   # 提示词要求"最近三个月"
    if not sr:
        return {"status": "partial", "data": None,
                "text": "波段转折点太少，无法聚类出可靠的支撑压力位。",
                "gap": _gap("p11")}

    sup, res = sr["supports"], sr["resistances"]
    def fmt(items: list[dict], kind: str) -> str:
        if not items:
            return f"（{kind}：未找到）"
        out = []
        for i in items:
            if i["touches"]:
                out.append(f"{i['price']}（被触碰 {i['touches']} 次，最近 {i['last_date']}）")
            else:
                out.append(f"{i['price']}（{i['basis']}，非历史聚集位）")
        return "；".join(out)

    text = (f"近 {sr['lookback']} 个交易日（约三个月）共识别到 {sr['pivots']} 个波段转折点，"
            f"聚成 {sr['clusters']} 个价位簇。现价 {sr['price']}。"
            f"**支撑位**：{fmt(sup, '支撑')}。**压力位**：{fmt(res, '压力')}。"
            f"口径说明：只有被反复验证过的价位才算支撑压力位，"
            f"单根长影线只是一个点。")
    for extra in (sr.get("sup_note"), sr.get("res_note")):
        if extra:
            text += extra

    return {
        "status": "grounded",
        "data": sr,
        "text": text,
        "gap": None,
        "source": SRC_KLINE,
        "method_caveat": ("支撑压力位是历史行为统计，不是物理定律。"
                          "放量突破时它们会失效，而且突破后角色会互换"
                          "（原压力变支撑）。"),
    }


def _ans_p12(ctx: dict) -> dict[str, Any]:
    bars = ctx.get("bars") or []
    q = ctx.get("quote") or {}
    price = _f(q.get("price")) or (_f(bars[-1].get("close")) if bars else None)
    box = ctx.get("box") or {}
    r = scenarios(bars, price, box.get("analysis") if isinstance(box, dict) else None)
    if not r:
        return {"status": "partial", "data": None,
                "text": "K线不足 60 根，无法估计波动率。", "gap": _gap("p12")}

    text = (f"以现价 {r['price']} 为起点，近 60 日日均波动 {r['daily_vol_pct']}%，"
            f"推到 {r['horizon_days']} 个交易日（约三个月）一个标准差是 "
            f"{r['sigma_3m_pct']}%。三种情况的目标价都对齐了真实关键位"
            f"（均线、箱体上下沿、支撑压力位），不是纯统计数字。")
    return {
        "status": "grounded",
        "data": r,
        "text": text,
        "gap": None,
        "source": SRC_KLINE + " + 波动率模型",
        "method_caveat": r["caveat"],
    }


def _ans_p13(ctx: dict) -> dict[str, Any]:
    """综合建议：把前面算出来的机械指标汇总成短结论。

    ⚠️ 这不是投资建议。它只是把已经算出来的分数、位置、趋势
       用固定规则拼成一句话 —— 没有任何额外的「判断」。
    """
    panel = ctx.get("panel") or {}
    score = panel.get("score")
    verdict = panel.get("verdict") or "—"
    plan = ctx.get("plan") or {}
    box = (ctx.get("box") or {}).get("analysis") or {}
    tech = (ctx.get("tech") or {}).get("values") or {}
    q = ctx.get("quote") or {}
    price = _f(q.get("price"))
    ma20 = _f(tech.get("ma20"))

    # 仓位规则：由评分机械映射，不含主观判断
    if score is None:
        pos, pos_reason = "无法给出", "综合评分缺失"
    elif score >= 6.5:
        pos, pos_reason = "不超过 30%", f"综合评分 {score}（偏高）"
    elif score >= 5.5:
        pos, pos_reason = "不超过 20%", f"综合评分 {score}（中性偏多）"
    elif score >= 4.5:
        pos, pos_reason = "不超过 10%", f"综合评分 {score}（中性）"
    else:
        pos, pos_reason = "暂不参与 / 0%", f"综合评分 {score}（偏弱）"

    action = "观察"
    if score is not None:
        if score >= 5.5:
            action = "持有"
        elif score >= 4.5:
            action = "持有或小仓位试探"
        else:
            action = "减持 / 观望"

    bits = []
    if price:
        bits.append(f"现价 {price}")
    if ma20:
        bits.append(f"{'站上' if price and price >= ma20 else '位于'} MA20"
                    if price else f"MA20 {ma20}")
    if box.get("position_pct") is not None:
        bits.append(f"箱体位置 {box['position_pct']}%（{box.get('zone', '')}）")
    head = "，".join(bits) + "。" if bits else ""

    text = (f"{head}综合评分 {score}，倾向「{action}」，"
            f"参考仓位 {pos}（{pos_reason}）。"
            f"以上为机械汇总，不构成买卖建议，决策请自行判断。")
    # 严格控制在 100 字以内（提示词要求）
    if len(text) > 100:
        text = (f"现价 {price}，综合评分 {score}，倾向「{action}」，"
                f"参考仓位 {pos}。机械汇总，非投资建议。")

    return {
        "status": "grounded",
        "data": {"score": score, "verdict": verdict, "action": action,
                 "position": pos, "position_reason": pos_reason,
                 "plan": plan, "chars": len(text)},
        "text": text,
        "gap": None,
        "source": "本系统综合评分模型（技术面 50% + 基本面 35% + 估值 15%）",
        "method_caveat": ("仓位的百分比来自评分区间的机械映射，"
                          "没有考虑你的总资产、风险承受能力和已有持仓。"
                          "真实仓位管理必须结合这些个人因素。"),
    }


HANDLERS = {
    "p3": _ans_p3, "p4": _ans_p4, "p5": _ans_p5, "p6": _ans_p6,
    "p7": _ans_p7, "p10": _ans_p10, "p11": _ans_p11,
    "p12": _ans_p12, "p13": _ans_p13,
}


# ============================================================
# 入口
# ============================================================

def build(symbol: str) -> dict[str, Any]:
    """跑完整的 6 步 13 提示词。"""
    sym = market.normalize(symbol)
    ctx: dict[str, Any] = {"symbol": sym}

    # 数据采集：任何一项失败都不该让整份分析挂掉
    try:
        ctx["quote"] = market.get_quote(sym) or {}
        ctx["name"] = ctx["quote"].get("name") or market.display_name(sym)
    except Exception as exc:  # noqa: BLE001
        log.warning("flow: 行情获取失败 %s: %s", sym, exc)
        ctx["quote"], ctx["name"] = {}, market.display_name(sym)

    # 只取一次 K线：长历史（供 PE 分位用）拿回来后就地切片给分析用。
    # 之前这里是 get_kline(800) + long_history(1300) 两次调用 ——
    # 两者是不同的缓存键，冷启动时会各打一遍数据源，实测把整页拖到 11.5 秒。
    try:
        long_bars = long_history(sym, 1300)
        ctx["long_bars"] = long_bars
        ctx["bars"] = long_bars[-800:]
    except Exception as exc:  # noqa: BLE001
        log.warning("flow: K线获取失败 %s: %s", sym, exc)
        ctx["bars"] = []

    if ctx["bars"]:
        try:
            ctx["tech"] = ta.latest_snapshot(ctx["bars"])
        except Exception as exc:  # noqa: BLE001
            log.warning("flow: 指标计算失败 %s: %s", sym, exc)
            ctx["tech"] = {}

    try:
        ctx["fundamentals"] = market.get_fundamentals(sym)
    except Exception as exc:  # noqa: BLE001
        log.warning("flow: 财务数据获取失败 %s: %s", sym, exc)
        ctx["fundamentals"] = None

    # 箱体（提示词十二要用箱体上下沿做悲观情景的目标位）
    if ctx["bars"]:
        try:
            from . import box as _box
            ctx["box"] = {"analysis": _box.adaptive(ctx["bars"],
                                                    (ctx["quote"] or {}).get("price"))
                          .get("analysis")}
        except Exception as exc:  # noqa: BLE001
            log.debug("flow: 箱体分析失败 %s: %s", sym, exc)
            ctx["box"] = {}

    # 综合评分（提示词十三要用）
    try:
        from . import panel as _panel
        ctx["panel"] = _panel.composite_score(ctx)
        ctx["plan"] = _panel.trading_plan(ctx["bars"], ctx) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("flow: 综合评分失败 %s: %s", sym, exc)
        ctx["panel"] = {}

    answers = []
    for p in PROMPTS:
        pid = p["id"]
        base = {"id": pid, "step": p["step"], "seq": PROMPTS.index(p) + 1,
                "title": p["title"], "prompt": p["prompt"], "status": p["status"]}
        fn = HANDLERS.get(pid)
        if fn is None:
            base.update({"data": None, "text": None, "gap": _gap(pid),
                         "source": None})
            answers.append(base)
            continue
        try:
            r = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            log.error("flow: 提示词 %s 执行失败: %s", pid, exc, exc_info=True)
            r = {"status": "partial", "data": None,
                 "text": f"这一项计算失败：{exc}", "gap": None}
        base.update(r)
        answers.append(base)

    steps = []
    for n in sorted(STEP_TITLES):
        items = [a for a in answers if a["step"] == n]
        steps.append({
            "n": n, "title": STEP_TITLES[n],
            "count": len(items),
            "grounded": sum(1 for a in items if a["status"] == "grounded"),
            "partial": sum(1 for a in items if a["status"] == "partial"),
            "no_data": sum(1 for a in items if a["status"] == "no_data"),
            "answers": items,
        })

    total = len(answers)
    grounded = sum(1 for a in answers if a["status"] == "grounded")
    return {
        "symbol": sym,
        "name": ctx.get("name"),
        "price": (ctx.get("quote") or {}).get("price"),
        "pct_change": (ctx.get("quote") or {}).get("pct_change"),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total": total, "grounded": grounded,
            "partial": sum(1 for a in answers if a["status"] == "partial"),
            "no_data": sum(1 for a in answers if a["status"] == "no_data"),
        },
        "steps": steps,
        "disclaimer": (
            "本栏目只做两件事：把系统里**真实存在的数据**取出来算好，"
            "以及明确指出**哪些信息本系统没有**。"
            "标记为「无数据」的 5 项不会生成任何结论 —— "
            "AI 在没有数据时会编出一个像模像样的答案，那比空白更危险。"
            "所有内容不构成投资建议。"
        ),
    }


def data_pack(symbol: str) -> str:
    """把 13 个提示词 + 本系统的真实数据拼成一段可直接复制的文本。

    这是给"想拿去外部 AI 跑一遍"的用户准备的：
    提示词原文不变，但把真实数据一起带上，
    这样外部 AI 就不需要（也不会）去编造这些数字。
    """
    d = build(symbol)
    lines: list[str] = []
    lines.append(f"# {d['name']}（{d['symbol']}）全流程分析数据包")
    lines.append(f"# 生成时间：{d['generated_at']}    现价：{d.get('price')}")
    lines.append("# 以下数据由 StockLab 从行情与财务接口取得，可直接作为分析依据。")
    lines.append("# 标注【本系统无此数据】的项目请自行补充，不要让 AI 猜。")
    lines.append("")

    for st in d["steps"]:
        lines.append(f"## {st['title']}")
        lines.append("")
        for a in st["answers"]:
            lines.append(f"### 提示词{a['seq']}（{a['title']}）")
            lines.append(f"{a['prompt']}")
            lines.append("")
            if a["status"] == "no_data":
                lines.append("【本系统无此数据】" + (a.get("gap") or {}).get("reason", ""))
                for w in (a.get("gap") or {}).get("where", []):
                    lines.append(f"  - 可查：{w}")
            else:
                if a.get("text"):
                    lines.append(f"数据结论：{a['text']}")
                data = a.get("data")
                if data:
                    lines.append(f"结构化数据：{_json_brief(data)}")
                if a.get("source"):
                    lines.append(f"数据来源：{a['source']}")
                if a.get("method_caveat"):
                    lines.append(f"口径说明：{a['method_caveat']}")
            lines.append("")
    lines.append("## 使用说明")
    lines.append("把上面每个提示词连同它的数据一起发给 AI。")
    lines.append("标注【本系统无此数据】的部分，AI 若给出具体数字即为编造。")
    return "\n".join(lines)


def _json_brief(obj: Any, limit: int = 1200) -> str:
    import json
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= limit else s[:limit] + "…（已截断）"
