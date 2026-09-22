"""异动归因：发生了什么 → 正在发生什么 → 什么条件下会怎样。

## 为什么不是加权求和

系统里原有的「综合评分」是加权求和（技术 50% + 基本面 35% + 估值 15%）。
加权求和有个天生缺陷：**它会把矛盾信号抹平**。

「基本面差 + 资金猛进 + 位置在箱底」和「基本面好 + 资金流出 + 位置在箱顶」，
加权后可能都落在 5.2 分附近。但这两种情形的风险收益完全相反 ——
前者可能是机会，后者可能是陷阱。**求和的本质是"这些因素可以互相替代"，
而现实里它们互为条件。**

所以本模块用**条件树**：先判状态（阶段 / 资金 / 位置 / 量能），
再由状态组合决定含义。同样的技术形态，含义取决于资金和位置。

## 三层结构

  第 0 层  有没有异动      —— 用**这只股票自己的波动率**做基准算 z-score
  第 1 层  发生了什么      —— 大盘 / 板块 / 资金 / 位置 / 消息，逐维拆开
  第 2 层  正在发生什么    —— 阶段 + 资金 + 位置 + 量能 → 状态判定
  第 3 层  什么条件下会怎样 —— 关键位 + 触发条件 + 失效位（不给预测）

## 关于异动检测为什么要用 z-score

绝对涨跌幅没有可比性：一只日常波动 3% 的股票涨 3% 很平常，
一只日常波动 0.5% 的银行股涨 3% 才是真异动。
所以基准必须是它自己的历史波动，而不是一个固定百分比。
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from ..sources import eastmoney as em
from ..sources import market
from ..sources import news as news_src

log = logging.getLogger("stocklab.anomaly")

# 相关指数：个股涨跌要和它比，才能算「超额」
BENCH = [("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指")]

# 异动等级阈值（按 z-score）
Z_MILD, Z_OBVIOUS, Z_STRONG = 1.5, 2.5, 4.0
# 放量倍数阈值
VOL_MILD, VOL_OBVIOUS = 1.5, 2.5


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _pick_bench(symbol: str) -> tuple[str, str]:
    s = symbol.upper()
    if s.endswith(".SZ"):
        return "399001.SZ", "深证成指"
    if s.endswith(".SH"):
        return "000001.SH", "上证指数"
    return "000001.SH", "上证指数"


# ============================================================
# 第 0 层：异动检测
# ============================================================

def detect(bars: list[dict], quote: dict) -> dict[str, Any]:
    """判断今天有没有异动。基准是这只股票自己的波动率。"""
    if len(bars) < 30:
        return {"ok": False, "reason": "K线不足 30 根，无法建立波动率基准"}

    closes = [_f(b.get("close")) or 0 for b in bars]
    vols = [_f(b.get("volume")) or 0 for b in bars]
    highs = [_f(b.get("high")) or 0 for b in bars]
    lows = [_f(b.get("low")) or 0 for b in bars]
    opens = [_f(b.get("open")) or 0 for b in bars]

    # 日收益率序列（用最近 60 个交易日建立基准，不含今天）
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append((closes[i] / closes[i - 1] - 1) * 100)
    base = rets[-61:-1] if len(rets) > 61 else rets[:-1]
    if len(base) < 20:
        return {"ok": False, "reason": "历史波动样本不足"}
    sd = _std(base)
    if sd <= 0:
        return {"ok": False, "reason": "历史波动率为 0，无法计算"}

    pct = _f(quote.get("pct_change"))
    if pct is None and closes[-2]:
        pct = (closes[-1] / closes[-2] - 1) * 100
    z = pct / sd if pct is not None else None

    vol_ratio = None
    if len(vols) >= 21 and vols[-1]:
        avg20 = sum(vols[-21:-1]) / 20
        if avg20 > 0:
            vol_ratio = vols[-1] / avg20

    prev_close = _f(quote.get("prev_close")) or (closes[-2] if len(closes) > 1 else None)
    amplitude = None
    if prev_close and highs[-1] and lows[-1]:
        amplitude = (highs[-1] - lows[-1]) / prev_close * 100
    gap = None
    if prev_close and opens[-1]:
        gap = (opens[-1] - prev_close) / prev_close * 100

    # 位置：是否创 N 日新高/新低
    hi_60, lo_60 = max(highs[-61:-1]), min(lows[-61:-1])
    new_high = highs[-1] > hi_60 if highs[-1] else False
    new_low = lows[-1] < lo_60 if lows[-1] else False

    # 综合等级：涨幅和放量都算，取更严重的那个
    lv = 0
    if z is not None:
        if abs(z) >= Z_STRONG:
            lv = 3
        elif abs(z) >= Z_OBVIOUS:
            lv = 2
        elif abs(z) >= Z_MILD:
            lv = 1
    if vol_ratio:
        vl = 3 if vol_ratio >= VOL_OBVIOUS * 1.6 else (2 if vol_ratio >= VOL_OBVIOUS else
                                                       (1 if vol_ratio >= VOL_MILD else 0))
        lv = max(lv, vl)
    if new_high and (z or 0) > Z_MILD:
        lv = max(lv, 2)

    names = {0: "无异常", 1: "轻微", 2: "明显", 3: "剧烈"}
    reasons = []
    if z is not None and abs(z) >= Z_MILD:
        reasons.append(f"涨跌幅 {pct:+.2f}% 是其日常波动（{sd:.2f}%）的 {abs(z):.1f} 倍")
    if vol_ratio and vol_ratio >= VOL_MILD:
        reasons.append(f"成交量为近 20 日均量的 {vol_ratio:.2f} 倍")
    if amplitude and amplitude >= 2 * max(sd, 0.5) * 1.5:
        reasons.append(f"振幅 {amplitude:.2f}%（日常波动 {sd:.2f}%）")
    if new_high:
        reasons.append("创近 60 日新高")
    if new_low:
        reasons.append("创近 60 日新低")

    return {
        "ok": True,
        "level": lv,
        "level_name": names[lv],
        "pct_change": None if pct is None else round(pct, 2),
        "z_score": None if z is None else round(z, 2),
        "daily_vol_pct": round(sd, 2),
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
        "amplitude": None if amplitude is None else round(amplitude, 2),
        "gap": None if gap is None else round(gap, 2),
        "new_high_60": new_high,
        "new_low_60": new_low,
        "reasons": reasons,
        "is_anomaly": lv >= 1,
    }


# ============================================================
# 第 1 层：归因
# ============================================================

def attribute(symbol: str, bars: list[dict], quote: dict,
              peers: list[dict] | None = None,
              fund: list[dict] | None = None,
              msgs: dict | None = None) -> dict[str, Any]:
    """把异动拆成互相独立的几维，逐维对照。

    刻意**不合并成一个分数** —— 每一维的含义不同，合并就丢掉了信息。
    """
    out: dict[str, Any] = {}
    pct = _f(quote.get("pct_change")) or 0.0

    # ---- 维度 1：大盘 ----
    bench_code, bench_name = _pick_bench(symbol)
    bench_pct = None
    try:
        qs = market.get_quotes([b for c, _ in BENCH for b in [c]])
        row = qs.get(bench_code) or {}
        bench_pct = _f(row.get("pct_change"))
        out["bench_all"] = {c: {"name": n, "pct": _f((qs.get(c) or {}).get("pct_change"))}
                            for c, n in BENCH}
    except Exception as exc:  # noqa: BLE001
        log.debug("指数获取失败: %s", exc)
    out["bench"] = {"code": bench_code, "name": bench_name, "pct": bench_pct}
    out["excess_vs_bench"] = None if bench_pct is None else round(pct - bench_pct, 2)

    # ---- 维度 2：板块（同业共振还是独立行情）----
    peer_rows = []
    if peers:
        codes = [p.get("code") for p in peers if p.get("code")]
        codes = [c if "." in c else (c + (".SH" if c.startswith(("6", "5")) else ".SZ"))
                 for c in codes][:6]
        try:
            qs = market.get_quotes(codes)
            for p in peers:
                c = p.get("code") or ""
                cc = c if "." in c else (c + (".SH" if c.startswith(("6", "5")) else ".SZ"))
                row = qs.get(cc) or {}
                v = _f(row.get("pct_change"))
                if v is not None:
                    peer_rows.append({"name": p.get("name") or row.get("name"), "pct": v})
        except Exception as exc:  # noqa: BLE001
            log.debug("同业行情获取失败: %s", exc)
    out["peers"] = peer_rows
    if peer_rows:
        vals = sorted(x["pct"] for x in peer_rows)
        med = vals[len(vals) // 2]
        out["peer_median"] = round(med, 2)
        out["excess_vs_peers"] = round(pct - med, 2)
        # 同业中位接近 0 而本股大涨 → 独立行情，不是板块带动
        up = sum(1 for x in peer_rows if x["pct"] > 1)
        out["peers_up"] = up
        out["peers_total"] = len(peer_rows)

    # ---- 维度 3：资金 ----
    if fund:
        mains = [_f(x.get("main")) or 0 for x in fund]
        out["fund"] = {
            "source": fund[-1].get("source"),
            "today": mains[-1] if mains else None,
            "last5": mains[-5:],
            "sum5": sum(mains[-5:]),
            "sum20": sum(mains[-20:]),
            "sum60": sum(mains[-60:]) if len(mains) >= 60 else None,
            "positive_days_20": sum(1 for v in mains[-20:] if v > 0),
            "positive_days_60": sum(1 for v in mains[-60:] if v > 0) if len(mains) >= 60 else None,
            "days": len(mains),
        }
        # 持续性判定：用「净额 / 绝对额」的强度比，而不是数天数。
        # 数天数太粗：实测歌尔股份 20 日净流入只有 11/20 天，
        # 但 5 日 +16.8 亿、20 日 +15.0 亿，明明是持续流入 ——
        # 因为流入的日子金额大、流出的日子金额小，天数占比看不出来。
        w = mains[-20:]
        gross = sum(abs(v) for v in w)
        intensity = (sum(w) / gross) if gross > 0 else 0.0
        out["fund"]["intensity_20"] = round(intensity, 3)
        if intensity >= 0.15:
            out["fund"]["trend"] = "持续净流入"
        elif intensity <= -0.15:
            out["fund"]["trend"] = "持续净流出"
        else:
            out["fund"]["trend"] = "反复"

    # ---- 维度 4：位置 ----
    #
    # ⚠️ 「位置」必须说明是**哪个区间**的位置，否则会误导：
    # 实测歌尔股份在 250 日区间处于 22.5%（偏低），但在 60 日区间是新高 ——
    # 也就是"近一年低位、但近期最强"。只给一个数字，
    # 说"低位"会让人误以为还没涨，说"高位"又丢掉了它跌了一年的背景。
    if bars:
        out["position"] = _positions(bars)


    # ---- 维度 5：消息（只报事实 + 时间是否吻合）----
    if msgs is not None:
        out["news"] = _timeline(msgs)

    return out


def _timeline(msgs: dict, move_date: str | None = None) -> dict[str, Any]:
    """判断消息和异动在**时间上**是否吻合。

    这里有个容易做错的地方：收盘后发布的调研纪要、晚间新闻，
    **解释不了盘中那波上涨**。所以必须比时间，不能只看"今天有没有消息"。

    实测歌尔股份就踩过：当天 18:33、19:14 发的新闻，
    而股价异动发生在盘中 —— 时间上不成立。
    """
    today = move_date or dt.date.today().isoformat()
    anns = msgs.get("announcements") or []
    nws = msgs.get("news") or []

    today_anns = [x for x in anns if x.get("date") == today]
    today_news = [x for x in nws if str(x.get("time") or "")[:10] == today]

    # 收盘时间 15:00，之后发布的新闻对当日盘中走势不构成解释
    intraday_news, after_close_news = [], []
    for x in today_news:
        t = str(x.get("time") or "")
        hhmm = t[11:16] if len(t) >= 16 else ""
        (after_close_news if hhmm and hhmm >= "15:00" else intraday_news).append(x)

    return {
        "move_date": today,
        "today_announcements": today_anns,
        "today_news": today_news,
        "intraday_news": intraday_news,
        "after_close_news": after_close_news,
        "recent_announcements": anns[:5],
        "recent_news": nws[:5],
        "errors": msgs.get("errors") or [],
    }


# ============================================================
# 第 2 层：状态判定（条件树，不是加权）
# ============================================================

# 条件表：阶段 × 资金 × 位置 → 含义
# 写死成表而不是加权，是因为这些组合的含义**不能互相替代**：
# 「箱体 + 资金流入 + 箱底」和「箱体 + 资金流出 + 箱顶」不是同一个东西的两个侧面，
# 而是方向完全相反的两种局面。
PHASE_RULES = [
    # (阶段, 资金, 位置, 标题, 含义)
    ("箱体震荡", "持续净流入", "低位",
     "箱底吸筹",
     "价格在箱体下沿、资金却持续净流入 —— 这是吸筹的典型组合。"
     "但要注意：这只是「资金在买」的事实，不等于一定会向上突破。"
     "真正的验证是价格能否站上中轴并守住。"),
    ("箱体震荡", "持续净流入", "高位",
     "冲向箱顶",
     "价格已到箱体上沿、资金仍在流入 —— 有突破的可能，也有冲高不破回落的风险。"
     "关键看能否**放量站稳**箱顶之上；缩量冲高然后掉回箱内的，通常是假突破。"),
    ("箱体震荡", "持续净流出", "高位",
     "箱顶派发",
     "价格在箱体上沿、资金却在流出 —— 这是需要警惕的组合："
     "股价位置好但资金在走，往往意味着有人在借高位出货。"),
    ("箱体震荡", "持续净流出", "低位",
     "箱底无人接",
     "价格在箱体下沿、资金还在流出 —— 箱底的支撑可能靠不住。"
     "「跌到箱底就该买」在资金持续流出的情况下并不成立。"),
    ("上升通道", "持续净流入", "低位",
     "趋势回调买点区",
     "趋势向上、资金持续流入、价格回到通道下沿 —— 顺着趋势的回调。"
     "失效条件是有效跌破通道下沿，那时趋势判断本身要推翻。"),
    ("上升通道", "持续净流入", "高位",
     "趋势延续但位置偏高",
     "趋势和资金同向，但价格已在通道上沿。追高的风险来自回调幅度，"
     "而不是趋势方向 —— 要想清楚能承受多大回撤。"),
    ("上升通道", "持续净流出", "高位",
     "量价背离",
     "价格在涨、资金在走 —— 这是**背离**。上涨由少数资金推动、"
     "主力在减，这种上涨的持续性通常较差，要提防突然的补跌。"),
    ("下降通道", "持续净流入", "低位",
     "下跌中的承接",
     "趋势向下但资金开始流入 —— 可能是抄底资金，也可能是下跌中继。"
     "在趋势没走平之前，「资金流入」的胜率并不高，需要价格先止跌确认。"),
    ("下降通道", "持续净流出", "低位",
     "趋势与资金同向向下",
     "趋势向下、资金流出、位置在低位 —— 三者同向，没有出现任何转折信号。"
     "这种时候「便宜」不构成买入理由。"),
    ("下降通道", "持续净流出", "高位",
     "下跌初期",
     "下降趋势 + 资金流出 + 位置还不低 —— 风险尚未释放完。"),
    ("方向不明", "持续净流入", "低位", "资金先行",
     "形态还没走出来，但资金已持续流入且位置不高 —— 属于「资金先行」的观察窗口，"
     "此时形态未确认，仓位通常应比形态确认后更轻。"),
    ("箱体震荡", "反复", "低位",
     "箱底附近，资金未形成方向",
     "价格在箱体下沿，但资金时进时出、没有明确方向。"
     "这种组合下的箱底支撑属于「没有资金背书」的支撑，"
     "观察点是能否出现连续净流入，以及价格能否站稳中轴。"),
    ("箱体震荡", "反复", "高位",
     "箱顶附近，资金未形成方向",
     "价格在箱体上沿而资金反复 —— 突破需要资金配合，"
     "资金没方向时冲高回落是常态。"),
    ("箱体震荡", "反复", "中位",
     "箱体中部，资金无方向",
     "价格在箱体中部、资金也没有方向，属于典型的「没有信息」状态。"
     "中轴附近上下空间相当，此时做多做空都缺少依据。"),
    ("上升通道", "反复", "低位",
     "趋势中的回调，资金观望",
     "上升趋势里回调到通道下沿，资金暂时没有跟进。"
     "趋势仍在，但缺少资金确认，需要等资金转向或价格明确止跌。"),
    ("上升通道", "反复", "高位",
     "趋势高位，资金观望",
     "价格在上升通道上沿、资金反复，说明追高意愿不足。"),
    ("下降通道", "反复", "低位",
     "弱势反弹或止跌观察",
     "下降趋势中资金反复、位置偏低 —— 可能是止跌，也可能只是下跌中继。"
     "趋势没走平之前不构成买入依据。"),
    ("方向不明", "反复", "低位",
     "方向不明，资金也无方向",
     "形态和资金都没有方向，没有可操作的信号。"),
    ("方向不明", "反复", "高位",
     "方向不明，位置偏高",
     "形态不明但位置偏高，风险大于机会。"),
    ("方向不明", "持续净流出", "低位", "资金离场",
     "形态不明、资金持续流出 —— 没有值得参与的理由，等待更清晰的信号。"),
]

VOLUME_PRICE = [
    ("放量上涨", "资金流入", "量价资金三者同向，是健康的上攻形态"),
    ("放量上涨", "资金流出", "价涨量增但资金在流出 —— 拉高出货的典型嫌疑，要警惕"),
    ("缩量上涨", "任意", "缩量上涨可能是惜售（好事）也可能是无量空涨（假象），需看后续能否放量"),
    ("放量下跌", "资金流出", "放量下跌 + 资金流出，是明确的派发信号"),
    ("缩量下跌", "任意", "缩量回调通常是正常调整，但如果趋势已破则另当别论"),
]


def state(quote: dict, tech: dict, det: dict, attr: dict,
          bars: list[dict], box: dict | None = None) -> dict[str, Any]:
    """把各维拼成「现在处于什么状态」+ 条件式含义。"""
    v = (tech or {}).get("values") or {}
    closes = [_f(b.get("close")) or 0 for b in bars] if bars else []
    price = _f(quote.get("price")) or (closes[-1] if closes else None)

    # 阶段：用均线排列 + MA60 斜率
    ma5, ma20, ma60 = _f(v.get("ma5")), _f(v.get("ma20")), _f(v.get("ma60"))
    slope = None
    if len(closes) >= 80:
        a, b = sum(closes[-60:]) / 60, sum(closes[-80:-20]) / 60
        if b:
            slope = (a / b - 1) * 100
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60 and (slope is None or slope > -0.5):
            phase = "上升通道"
        elif ma5 < ma20 < ma60 and (slope is None or slope < 0.5):
            phase = "下降通道"
        elif slope is not None and abs(slope) < 1.0:
            phase = "箱体震荡"
        else:
            phase = "方向不明"
    else:
        phase = "方向不明"

    # 位置的区间要跟阶段匹配：
    #   箱体震荡 → 看**箱体**里的位置（箱体本身就是近期的事）
    #   趋势通道 → 看 250 日区间的位置
    # 用错区间会得出相反结论：同一天可以是"近一年 22% 的低位"
    # 同时又是"近 60 日 100% 的新高"。
    posinfo = attr.get("position") or {}
    box_pos = (box or {}).get("position_pct") if box else None
    if phase == "箱体震荡" and box_pos is not None:
        pos_raw, pos_scope = box_pos, "箱体内"
    else:
        pos_raw, pos_scope = posinfo.get("pct_250"), "250日区间"
    if pos_raw is None:
        position = "中位"
    elif pos_raw <= 30:
        position = "低位"
    elif pos_raw >= 70:
        position = "高位"
    else:
        position = "中位"

    funding = (attr.get("fund") or {}).get("trend") or "无数据"

    # 量价配合
    vr = det.get("vol_ratio")
    pct = det.get("pct_change") or 0
    fw = (attr.get("fund") or {}).get("today") or 0
    if vr is None:
        vp = "无数据"
    elif pct > 0.5:
        vp = "放量上涨" if vr >= 1.5 else "缩量上涨"
    elif pct < -0.5:
        vp = "放量下跌" if vr >= 1.5 else "缩量下跌"
    else:
        vp = "横盘"
    fund_dir = "资金流入" if fw > 0 else ("资金流出" if fw < 0 else "资金持平")

    hit = None
    for ph, fu, po, title, meaning in PHASE_RULES:
        if ph == phase and fu == funding and po == position:
            hit = {"title": title, "meaning": meaning}
            break
    if hit is None:
        for ph, fu, po, title, meaning in PHASE_RULES:
            if ph == phase and fu == funding:
                hit = {"title": title, "meaning": meaning + f"（位置：{position}）"}
                break
    if hit is None:
        hit = {"title": "未匹配到特定组合",
               "meaning": (f"当前阶段「{phase}」、资金「{funding}」、位置「{position}」，"
                           f"没有落在已知的条件组合里 —— 这种情况本系统不下判断。")}

    vp_note = None
    for vpk, fk, note in VOLUME_PRICE:
        if vp == vpk and (fk == fund_dir or fk == "任意"):
            vp_note = note
            break

    return {
        "phase": phase,
        "position": position,
        "position_pct": pos_raw,
        "position_scope": pos_scope,
        "positions": posinfo,
        "funding": funding,
        "volume_price": vp,
        "fund_direction": fund_dir,
        "volume_note": vp_note,
        "condition": hit,
        "ma": {"ma5": ma5, "ma20": ma20, "ma60": ma60, "ma60_slope_20d": None if slope is None else round(slope, 2)},
        "price": price,
    }


# ============================================================
# 入口
# ============================================================

def analyze(symbol: str, with_news: bool = True) -> dict[str, Any]:
    sym = market.normalize(symbol)
    quote: dict = {}
    bars: list[dict] = []
    tech: dict = {}
    try:
        quote = market.get_quote(sym) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("anomaly: 行情失败 %s: %s", sym, exc)
    try:
        bars = market.get_kline(sym, "day", 300)
        from . import indicators as ta
        tech = ta.latest_snapshot(bars) if bars else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("anomaly: K线失败 %s: %s", sym, exc)

    det = detect(bars, quote)

    # 同业（板块维度）
    peers: list[dict] = []
    try:
        from ..sources import eastmoney_f10 as f10
        if f10.available(sym):
            ind = f10.industry(sym)
            peers = (ind.get("valuation") or [])[:5] or (ind.get("growth") or [])[:5]
    except Exception as exc:  # noqa: BLE001
        log.debug("anomaly: 行业失败 %s: %s", sym, exc)

    fund: list[dict] = []
    try:
        fund = em.fund_flow_any(sym, 120)
    except Exception as exc:  # noqa: BLE001
        log.debug("anomaly: 资金流失败 %s: %s", sym, exc)

    msgs = None
    if with_news:
        try:
            msgs = news_src.digest(sym, quote.get("name"), days=5)
        except Exception as exc:  # noqa: BLE001
            log.debug("anomaly: 消息失败 %s: %s", sym, exc)
            msgs = {"ok": False, "errors": [str(exc)], "announcements": [], "news": []}

    box = None
    try:
        from . import box as box_svc
        box = box_svc.adaptive(bars, quote.get("price")).get("analysis")
    except Exception as exc:  # noqa: BLE001
        log.debug("anomaly: 箱体失败 %s: %s", sym, exc)

    attr = attribute(sym, bars, quote, peers=peers, fund=fund, msgs=msgs)
    st = state(quote, tech, det, attr, bars, box)

    return {
        "symbol": sym,
        "name": quote.get("name") or market.display_name(sym),
        "price": quote.get("price"),
        "pct_change": quote.get("pct_change"),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "detect": det,
        "attribution": attr,
        "state": st,
        "disclaimer": (
            "本栏目做的是**归因和状态刻画**，不是预测。"
            "「发生了什么」用真实数据对照（大盘/板块/资金/位置/消息时间线），"
            "「正在发生什么」用条件组合判定，「什么条件下会怎样」只给触发条件与失效位。"
            "同样的形态在不同资金和位置下含义相反 —— 所以这里不做加权的综合评分。"
            "所有内容不构成投资建议。"
        ),
    }


def _positions(bars: list[dict]) -> dict[str, Any]:
    """同时给出多个区间的分位。"""
    closes = [_f(b.get("close")) or 0 for b in bars]
    cur = closes[-1]

    def pos(n: int) -> float | None:
        seg = closes[-n:] if len(closes) >= n else closes
        hi, lo = max(seg), min(seg)
        if hi <= lo:
            return None
        return round((cur - lo) / (hi - lo) * 100, 1)

    return {
        "price": cur,
        "pct_60": pos(60),
        "pct_120": pos(120),
        "pct_250": pos(250),
        "high_250": max(closes), "low_250": min(closes),
    }
