"""AI 研究简报。

支持任意 OpenAI 兼容接口（DeepSeek / 通义 / Kimi / Ollama / vLLM 等），
只需在 .env 里改 SL_AI_BASE_URL 与 SL_AI_MODEL。

未配置 API key 时自动降级为「本地规则引擎简报」——把指标、财务、资金
数据整理成结构化结论。保证 NAS 上无外网依赖时功能不缺失。
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

import httpx

from .. import db
from ..config import settings
from ..sources import market
from . import indicators as ta

log = logging.getLogger("stocklab.ai")


def _fmt(v: Any, digits: int = 2, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f}亿{unit}"
        if abs(v) >= 1e4:
            return f"{v / 1e4:.2f}万{unit}"
        return f"{v:.{digits}f}{unit}"
    return str(v)


def gather_context(symbol: str) -> dict[str, Any]:
    """汇总生成简报所需的全部数据。"""
    ctx: dict[str, Any] = {"symbol": symbol}
    try:
        q = market.get_quote(symbol)
        ctx["quote"] = q
    except Exception as exc:  # noqa: BLE001
        ctx["quote"] = None
        ctx["quote_error"] = str(exc)

    try:
        bars = market.get_kline(symbol, "day", 260)
        ctx["bars"] = bars
        ctx["tech"] = ta.latest_snapshot(bars, None, ctx.get("quote")) if bars else {}
    except Exception as exc:  # noqa: BLE001
        ctx["bars"] = []
        ctx["tech"] = {}
        ctx["kline_error"] = str(exc)

    try:
        ctx["fundamentals"] = market.get_fundamentals(symbol)
    except Exception as exc:  # noqa: BLE001
        ctx["fundamentals"] = None
        ctx["fund_error"] = str(exc)

    return ctx


def _system_facts(ctx: dict[str, Any]) -> list[str]:
    """把系统各处**已经算好**的结论汇总成几行，喂给模型。

    每一项都容错：某个模块挂了就跳过那一行，不让整份报告失败。
    这些结论都是代码按固定规则算的，可复现；模型只负责解释。
    """
    sym = ctx["symbol"]
    q = ctx.get("quote") or {}
    bars = ctx.get("bars") or []
    out: list[str] = []

    # 综合评分
    try:
        from . import panel as panel_svc
        sc = panel_svc.composite_score(ctx)
        if sc:
            out.append(f"- 综合评分: {sc.get('score')} / 10（{sc.get('verdict')}）")
            detail = sc.get("detail") or sc.get("parts") or {}
            if isinstance(detail, dict) and detail:
                out.append("  分项: " + "；".join(f"{k} {v}" for k, v in list(detail.items())[:5]))
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 综合评分失败 %s", exc)

    # PE 历史分位 + 代码给出的机制判定（最关键的一条）
    try:
        from . import flow as flow_svc
        pe = flow_svc.pe_percentile(sym, q.get("pe_ttm"), ctx.get("fundamentals"))
        if pe.get("ok"):
            h = pe["history"]
            out.append(f"- PE 五年分位: {pe['percentile']}%（{pe['band']}）"
                       f"，区间 {h['min']}~{h['max']}，中位 {h['median']}，样本 {h['days']} 天")
            if pe.get("price_percentile") is not None:
                out.append(f"  价格分位: {pe['price_percentile']}%"
                           f"（价格分位与估值分位之差 = {round(pe['percentile']-pe['price_percentile'],1)}）")
            if pe.get("cross_note"):
                out.append(f"  **系统判定**: {pe['cross_note']}")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: PE 分位失败 %s", exc)

    # 箱体
    try:
        from . import box as box_svc
        bx = box_svc.adaptive(bars, q.get("price")) if bars else {}
        a = bx.get("analysis") or {}
        if a:
            out.append(f"- 箱体（{bx.get('recommended_window')} 日窗口）: "
                       f"{a['bottom']} ~ {a['top']}，高度 {a['height_pct']}%"
                       f"，当前位置 {a['position_pct']}%（{a['zone']}）")
            out.append(f"  形态: {a['shape']}，置信度 {a['confidence']}%"
                       f"，触顶 {a['touch_top']} 次 / 触底 {a['touch_bottom']} 次"
                       f"，箱内占比 {round((a.get('inside_ratio') or 0)*100)}%")
            if bx.get("trustworthy") is False:
                out.append(f"  **系统判定**: {bx.get('verdict')}")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 箱体失败 %s", exc)

    # 支撑压力位
    try:
        from . import flow as flow_svc
        sr = flow_svc.support_resistance(bars, q.get("price"), lookback=120)
        if sr:
            sup = "；".join(f"{s['price']}（触碰 {s['touches']} 次）" for s in sr["supports"])
            res = "；".join(f"{r['price']}（触碰 {r['touches']} 次）" for r in sr["resistances"])
            out.append(f"- 支撑位: {sup or '无'}    压力位: {res or '无'}")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 支撑压力失败 %s", exc)

    # 资金流（趋势是代码按强度比判定的，不是模型看数）
    try:
        from ..sources import eastmoney as em
        ff = em.fund_flow_any(sym, 20)
        if ff:
            mains = [x.get("main") or 0 for x in ff]
            gross = sum(abs(v) for v in mains) or 1
            strength = sum(mains) / gross
            trend = ("持续净流入" if strength >= 0.15 else
                     ("持续净流出" if strength <= -0.15 else "反复"))
            out.append(f"- 资金流（{ff[-1].get('source')}）: {trend}"
                       f"（强度 {strength:.2f}），近 20 日累计 {sum(mains)/1e8:+.2f} 亿"
                       f"，净流入 {sum(1 for v in mains if v > 0)}/{len(mains)} 天")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 资金流失败 %s", exc)

    # 异动归因的条件判定
    try:
        from . import anomaly as anom_svc
        d = anom_svc.analyze(sym, with_news=False)
        det, st, at = d.get("detect") or {}, d.get("state") or {}, d.get("attribution") or {}
        if det.get("ok"):
            out.append(f"- 异动: {det['level_name']}"
                       f"（{det.get('z_score')} 倍日常波动，量比 {det.get('vol_ratio')}）；"
                       f"大盘超额 {at.get('excess_vs_bench')}%，板块超额 {at.get('excess_vs_peers')}%")
            cond = st.get("condition") or {}
            if cond.get("title"):
                out.append(f"  状态: {st.get('phase')} / {st.get('position_scope','')}{st.get('position')}"
                           f" / {st.get('volume_price')}")
                out.append(f"  **系统判定（条件组合）**: 【{cond['title']}】{cond.get('meaning','')}")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 异动失败 %s", exc)

    # 历史类比
    try:
        from . import analogs as ag_svc
        ag = ag_svc.analyze(sym, bars)
        if ag.get("ok"):
            h20 = (ag.get("horizons") or {}).get("20", {})
            s20 = h20.get("signal") or {}
            out.append(f"- 历史类比（{ag.get('tier')}匹配，{ag.get('events')} 次同类情形）: "
                       f"20 日上涨占比 {s20.get('win_rate')}%，中位 {s20.get('median')}%，"
                       f"最差 {s20.get('worst')}%，独立事件 {s20.get('independent')} 个")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 历史类比失败 %s", exc)

    # 行业对比
    try:
        from ..sources import eastmoney_f10 as f10
        if f10.available(sym):
            ind = f10.industry(sym)
            if ind.get("ok"):
                avg, med = ind.get("avg") or {}, ind.get("median") or {}
                sc = ind.get("scale") or {}
                if avg:
                    out.append(f"- 行业平均: PE {avg.get('pe_ttm') and round(avg['pe_ttm'],1)}"
                               f"，PB {avg.get('pb') and round(avg['pb'],2)}")
                if med:
                    out.append(f"  行业中值: ROE {med.get('roe_avg')}%，净利率 {med.get('net_margin')}%")
                if sc.get("market_cap_rank"):
                    out.append(f"  行业排名: 市值第 {sc['market_cap_rank']:.0f}"
                               f"，营收第 {sc.get('revenue_rank')}"
                               f"，净利第 {sc.get('net_profit_rank')}")
    except Exception as exc:  # noqa: BLE001
        log.debug("facts: 行业失败 %s", exc)

    return out


def build_prompt(ctx: dict[str, Any]) -> str:
    sym = ctx["symbol"]
    q = ctx.get("quote") or {}
    t = (ctx.get("tech") or {}).get("values") or {}
    snap = ctx.get("tech") or {}
    f = ctx.get("fundamentals") or {}
    latest = f.get("latest") or {}
    hist = f.get("history") or []
    bars = ctx.get("bars") or []

    name = q.get("name") or sym
    lines: list[str] = []
    lines.append(f"# 标的基础信息")
    lines.append(f"名称: {name}  代码: {sym}")
    lines.append(f"最新价: {_fmt(q.get('price'))}  涨跌幅: {_fmt(q.get('pct_change'))}%")
    lines.append(f"今开/最高/最低/昨收: {_fmt(q.get('open'))} / {_fmt(q.get('high'))} / {_fmt(q.get('low'))} / {_fmt(q.get('prev_close'))}")
    lines.append(f"成交额: {_fmt(q.get('amount'), unit='元')}  换手率: {_fmt(q.get('turnover_rate'))}%  量比: {_fmt(q.get('vol_ratio'))}")
    lines.append(f"总市值: {_fmt(q.get('market_cap'), unit='元')}  流通市值: {_fmt(q.get('float_cap'), unit='元')}")
    lines.append(f"PE(TTM): {_fmt(q.get('pe_ttm'))}  PE(动): {_fmt(q.get('pe'))}  PB: {_fmt(q.get('pb'))}")
    if q.get("main_net_inflow") is not None:
        lines.append(f"主力净流入: {_fmt(q.get('main_net_inflow'), unit='元')} ({_fmt(q.get('main_net_pct'))}%)")

    lines.append("")
    lines.append("# 技术指标（日线）")
    for label, key in [
        ("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20"), ("MA60", "ma60"), ("MA120", "ma120"), ("MA250", "ma250"),
    ]:
        lines.append(f"{label}: {_fmt(t.get(key))}")
    lines.append(f"MACD: DIF={_fmt(t.get('dif'))} DEA={_fmt(t.get('dea'))} 柱={_fmt(t.get('macd'))}")
    lines.append(f"KDJ: K={_fmt(t.get('k'))} D={_fmt(t.get('d'))} J={_fmt(t.get('j'))}")
    lines.append(f"RSI: RSI6={_fmt(t.get('rsi6'))} RSI12={_fmt(t.get('rsi12'))} RSI24={_fmt(t.get('rsi24'))}")
    lines.append(f"BOLL: 上轨={_fmt(t.get('boll_upper'))} 中轨={_fmt(t.get('boll_mid'))} 下轨={_fmt(t.get('boll_lower'))}")
    lines.append(f"ATR14={_fmt(t.get('atr14'))} CCI14={_fmt(t.get('cci14'))} WR14={_fmt(t.get('wr14'))}")
    lines.append(f"技术面综合评分: {snap.get('score')} / 评级: {snap.get('rating')}")
    if snap.get("signals"):
        lines.append("技术信号: " + "; ".join(s["text"] for s in snap["signals"]))

    if bars:
        lines.append("")
        lines.append("# 近期走势（最近 10 个交易日）")
        for b in bars[-10:]:
            lines.append(
                f"{b['date']}  开{b.get('open')} 高{b.get('high')} 低{b.get('low')} "
                f"收{b.get('close')} 量{b.get('volume')}"
            )
        closes = [b["close"] for b in bars if b.get("close")]
        if len(closes) >= 20:
            r20 = (closes[-1] / closes[-20] - 1) * 100
            r60 = (closes[-1] / closes[-60] - 1) * 100 if len(closes) >= 60 else None
            lines.append(f"近20日涨跌: {r20:.2f}%" + (f"  近60日涨跌: {r60:.2f}%" if r60 is not None else ""))
            lines.append(f"近250日区间: {min(closes[-250:]):.2f} ~ {max(closes[-250:]):.2f}")

    if latest:
        lines.append("")
        lines.append("# 财务数据（最新报告期）")
        lines.append(f"报告期: {latest.get('report_name')} ({latest.get('report_date')})")
        lines.append(f"每股收益: {_fmt(latest.get('eps'))}  每股净资产: {_fmt(latest.get('bps'))}")
        lines.append(f"营业收入: {_fmt(latest.get('revenue'), unit='元')}  同比: {_fmt(latest.get('revenue_yoy'))}%")
        lines.append(f"归母净利润: {_fmt(latest.get('net_profit'), unit='元')}  同比: {_fmt(latest.get('profit_yoy'))}%")
        lines.append(f"ROE: {_fmt(latest.get('roe'))}%  毛利率: {_fmt(latest.get('gross_margin'))}%  净利率: {_fmt(latest.get('net_margin'))}%")
        lines.append(f"资产负债率: {_fmt(latest.get('debt_ratio'))}%  流动比率: {_fmt(latest.get('current_ratio'))}")
    if len(hist) > 1:
        lines.append("")
        lines.append("# 财务趋势（近几期营收/净利，单位元）")
        for h in hist[-6:]:
            lines.append(f"{h['report_date']}: 营收 {_fmt(h.get('revenue'))}  净利 {_fmt(h.get('net_profit'))}  ROE {_fmt(h.get('roe'))}%")

    # ---- 系统已算出的结论 ----
    #
    # 这一段是整个提示词里最重要的部分。
    # 之前只给原始数字，模型得自己从 PE / EPS / 利润率里推导出「是不是假便宜」
    # 这种机制判断 —— 而实测本地 7B/12.6G 模型在这一点上会给出**互相矛盾且错误**
    # 的结论（一个说"股价高估"、一个说"业绩下滑"、一个说"真便宜"）。
    # 而这些判断系统里本来就由代码算好了（规则明确、可复现）。
    # 所以改成：**代码给结论，模型负责解释和补充** —— 它没有编数字的空间，
    # 也不会把盈利高增误读成利空。
    facts = _system_facts(ctx)
    if facts:
        lines.append("")
        lines.append("# 系统已算出的结论（**不要重新推导，请在此基础上解释、补充和挑错**）")
        lines.extend(facts)

    body = "\n".join(lines)
    return (
        "你是一位严谨的 A股/港股 证券分析师。下面给你两部分内容："
        "**系统用代码算出的结论** 和 **支撑这些结论的原始数据**。\n\n"
        "要求：\n"
        "1. 严格基于给定数据，不要编造任何未提供的数字或消息面事件。\n"
        "2. 「系统已算出的结论」是代码按固定规则得出的，**请直接采信并展开解释**，"
        "不要推翻它另起炉灶；如果你认为它有问题，请明确指出哪一条、为什么。\n"
        "2. 数据缺失时明确写「数据缺失」，不要臆测。\n"
        "3. 结构如下（用 Markdown）：\n"
        "   ## 一句话结论\n"
        "   ## 基本面分析（估值水平、盈利能力、成长性、财务健康度）\n"
        "   ## 技术面分析（趋势、均线、指标、量能、关键价位）\n"
        "   ## 主要风险\n"
        "   ## 关注要点（列出 3 条可验证的观察指标）\n"
        "4. 明确标注关键支撑位与压力位（用给定指标推算）。\n"
        "5. 结尾附一句免责声明，说明不构成投资建议。\n"
        "6. 总长度 600~1000 字，语言专业、克制，避免情绪化表达。\n\n"
        "===== 数据开始 =====\n"
        f"{body}\n"
        "===== 数据结束 ====="
    )


def _local_report(ctx: dict[str, Any]) -> str:
    """本地规则引擎生成的分析面板。

    结构对齐常见的个股分析面板：综合评分 → 技术面 → 基本面 → 风险
    → 历史回测 → 总结。所有数字都来自真实数据，缺失一律写「—」。
    """
    from . import panel as panel_svc
    from . import backtest as bt_svc

    sym = ctx["symbol"]
    q = ctx.get("quote") or {}
    snap = ctx.get("tech") or {}
    v = snap.get("values") or {}
    bars = ctx.get("bars") or []
    name = q.get("name") or sym

    sc = panel_svc.composite_score(ctx)
    plan = panel_svc.trading_plan(bars, ctx)
    fund_rows = panel_svc.fundamental_table(ctx)
    drivers = panel_svc.growth_drivers(ctx)

    out: list[str] = []
    out.append(f"# {name}（{sym}）分析面板")
    out.append("")
    out.append(
        f"> 最新价 {_fmt(q.get('price'))}　当日 {_fmt(q.get('pct_change'))}%　"
        f"成交额 {_fmt(q.get('amount'), unit='元')}　"
        f"总市值 {_fmt(q.get('market_cap'), unit='元')}"
    )
    if bars:
        out.append(f"> K线区间 {bars[0]['date']} ~ {bars[-1]['date']}（{len(bars)} 根）")
    out.append("> ⚠️ 本内容为公开数据的程序化解读，**不构成任何投资建议**。"
               "交易位均为公式推算，据此操作风险自负。")
    out.append("")

    # ---------- 综合评分 ----------
    out.append(f"## ✅ 综合评分：{sc['score']}/10，结论【{sc['verdict']}】")
    out.append("")
    parts = [f"技术面 {sc['tech_score']}"]
    if sc["fund_score"] is not None:
        parts.append(f"基本面 {sc['fund_score']}")
    if sc["val_score"] is not None:
        parts.append(f"估值 {sc['val_score']}")
    out.append(f"评分构成：{' / '.join(parts)}　权重：{sc['weights']}")
    out.append("")
    out.append(f"> 档位标定：{panel_svc.CALIBRATION_NOTE}。"
               "即【看多】= 全市场前 15%，【中性】= 中间 30%，"
               "分数是相对位置而非绝对好坏。")
    out.append("")
    out.append("**评分明细**（每一项都是可核对的，不是黑箱数字）")
    out.append("")
    out.append("| 维度 | 判断 | 得分 |")
    out.append("|---|---|---|")
    for label, score, _w in sc["tech_items"]:
        out.append(f"| 技术 | {label} | {score:.1f} |")
    for label, score in sc["fund_items"]:
        out.append(f"| 基本面 | {label} | {score:.1f} |")
    for label, score in sc["val_items"]:
        out.append(f"| 估值 | {label} | {score:.1f} |")
    out.append("")

    # 核心一句话
    tech_good = sc["tech_score"] >= 6.0
    fund_good = (sc["fund_score"] or 5.0) >= 6.0
    val_cheap = (sc["val_score"] or 5.0) >= 6.0
    bits = []
    # 区分「没有数据」和「数据一般」—— 把无财报数据的 ETF 说成"基本面一般"
    # 是误导，用户会以为公司基本面平庸
    if sc["fund_score"] is None:
        bits.append("无财报数据")
    else:
        bits.append("基本面扎实" if fund_good else
                    ("基本面偏弱" if sc["fund_score"] < 5 else "基本面一般"))
    bits.append("技术面走强" if tech_good else
                ("技术面偏弱" if sc["tech_score"] < 5 else "技术面中性"))
    if sc["val_score"] is None:
        bits.append("无估值数据")
    else:
        bits.append("估值有优势" if val_cheap else "估值偏高")
    closing = {
        "看多": "趋势与基本面共振，可考虑逢回调分批参与",
        "谨慎看多": "方向偏多但估值或位置不占优，不宜追高，等回调更稳妥",
        "中性": "多空因素相当，建议观望或小仓位试错",
        "谨慎看空": "弱势特征明显，宜降低仓位、等待企稳信号",
        "看空": "趋势与基本面均偏弱，规避为主",
    }.get(sc["verdict"], "")
    out.append(f"**核心一句话：{('，'.join(bits))}；{closing}。**")
    out.append("")

    # ---------- 一、技术面 ----------
    out.append("## 一、技术面分析")
    out.append("")
    ma5, ma10, ma20, ma60 = (v.get(k) for k in ("ma5", "ma10", "ma20", "ma60"))
    price = q.get("price")
    if None not in (ma5, ma10, ma20):
        if ma5 > ma10 > ma20:
            trend = "多头排列，中长期趋势向上"
        elif ma5 < ma10 < ma20:
            trend = "空头排列，中长期趋势向下"
        else:
            trend = "交织，方向不明"
        above = [n for n, m in (("MA20", ma20), ("MA60", ma60)) if m and price and price > m]
        out.append(f"1. **均线系统**：MA5 {_fmt(ma5)} / MA10 {_fmt(ma10)} / "
                   f"MA20 {_fmt(ma20)} / MA60 {_fmt(ma60)}，{trend}。"
                   + (f"股价站上 {'、'.join(above)}。" if above else "股价位于主要均线之下。"))
    else:
        out.append("1. **均线系统**：数据不足")
    dif, dea, hist = v.get("dif"), v.get("dea"), v.get("macd")
    macd_txt = "数据不足"
    if None not in (dif, dea):
        if dif > dea and dif > 0:
            macd_txt = "零轴上方金叉，多头力量较强"
        elif dif > dea:
            macd_txt = "零轴下方金叉，属于弱势反弹"
        elif dif < dea and dif < 0:
            macd_txt = "零轴下方死叉，空头占优"
        else:
            macd_txt = "零轴上方死叉，高位转弱"
    out.append("2. **震荡指标**")
    out.append(f"   - MACD：{macd_txt}（DIF {_fmt(dif)}，DEA {_fmt(dea)}，柱 {_fmt(hist)}）")
    r6 = v.get("rsi6")
    if r6 is not None:
        if r6 > 80:
            zone = "超买，短期回调风险大"
        elif r6 > 70:
            zone = "偏高，接近超买"
        elif r6 >= 55:
            zone = "中性偏强"
        elif r6 >= 45:
            zone = "中性"
        elif r6 >= 30:
            zone = "偏弱，接近超卖"
        elif r6 >= 20:
            zone = "超卖区域"
        else:
            zone = "严重超卖"
        out.append(f"   - RSI6 = {_fmt(r6)}，{zone}")
    j = v.get("j")
    if j is not None:
        zone = "已经超买，短期有回调风险" if j > 100 else ("超卖，存在反弹需求" if j < 0 else "处于正常区间")
        out.append(f"   - KDJ：J = {_fmt(j)}，{zone}")
    up, mid, low = v.get("boll_upper"), v.get("boll_mid"), v.get("boll_lower")
    if None not in (up, mid, low) and price:
        pos = (price - low) / (up - low) * 100 if up > low else 50
        where = "贴近上轨，上方压力明显" if pos > 80 else ("贴近下轨，下方有支撑" if pos < 20 else "位于通道中部")
        out.append(f"3. **布林带**：上轨 {_fmt(up)} / 中轨 {_fmt(mid)} / 下轨 {_fmt(low)}，"
                   f"当前位于通道 {pos:.0f}% 位置，{where}。")
    else:
        out.append("3. **布林带**：数据不足")
    out.append(f"   - 波动率 ATR14 = {_fmt(v.get('atr14'))}，5日量比 {_fmt(snap.get('vol_ratio_5'))}")
    if snap.get("signals"):
        out.append("   - 当前触发信号：" + "；".join(sk["text"] for sk in snap["signals"]))
    out.append("")

    # ---------- 技术位推算 ----------
    if plan:
        out.append("### 技术位推算（⚠️ 非买卖建议）")
        out.append("")
        out.append("> 以下价位完全由 ATR、布林带、均线、近 20/60 日高低点**机械推算**，"
                   "不包含任何对后市的判断，也**不构成买入或卖出建议**。仅供你判断「哪些位置值得关注」。")
        out.append("")
        out.append(f"- 现价：{plan['price']}")
        out.append(f"- 下方参考区（回踩关注）：**{plan['entry_low']} ~ {plan['entry_high']}**")
        out.append(f"- 参考止损位：**{plan['stop']}**（距参考区中值 {plan['risk_pct']}%）")
        out.append(f"- 第一目标：**{plan['target1']}**　第二目标：**{plan['target2']}**")
        if plan["rr1"]:
            rr = f"{plan['rr1']}:1"
            judge = "尚可" if plan["rr1"] >= 2 else ("偏低，性价比一般" if plan["rr1"] < 1.5 else "一般")
            out.append(f"- 盈亏比（到第一目标）：**{rr}**，{judge}")
        if plan["position_pct"]:
            out.append(f"- 按「单笔风险不超过总资金 1%」反推的仓位上限：**约 {plan['position_pct']}%**")
            out.append("  （这是风险控制的算术结果，不是让你买这么多）")
        out.append("")

    # ---------- 二、基本面 ----------
    out.append("## 二、基本面 & 财务数据")
    out.append("")
    out.append("| 指标 | 数据 | 解读 |")
    out.append("|---|---|---|")
    for label, val, words in fund_rows:
        out.append(f"| {label} | {val} | {words} |")
    out.append("")
    if drivers:
        out.append("**增长驱动（基于财报趋势归纳）**")
        out.append("")
        for i, d in enumerate(drivers, 1):
            out.append(f"{i}. {d}" if not d.startswith("（") else d)
        out.append("")

    # ---------- 三、风险 ----------
    out.append("## 三、风险点")
    out.append("")
    risks: list[str] = []
    latest = ((ctx.get("fundamentals") or {}).get("latest")) or {}
    if latest.get("profit_yoy") is not None and latest["profit_yoy"] < 0:
        risks.append(f"净利润同比下滑 {_fmt(latest['profit_yoy'])}%，盈利承压")
    if latest.get("revenue_yoy") is not None and latest["revenue_yoy"] < 0:
        risks.append(f"营业收入同比下滑 {_fmt(latest['revenue_yoy'])}%")
    if latest.get("debt_ratio") and latest["debt_ratio"] > 70:
        risks.append(f"资产负债率 {_fmt(latest['debt_ratio'])}% 偏高，需关注偿债能力")
    pe_now = q.get("pe_ttm") or q.get("pe")
    if pe_now and pe_now > 40:
        risks.append(f"估值显著偏高（PE {_fmt(pe_now)}），需要业绩持续兑现来消化")
    elif pe_now and pe_now > 25:
        risks.append(f"估值偏高（PE {_fmt(pe_now)}），安全边际有限")
    if j is not None and j > 100:
        risks.append("KDJ 超买，短期存在回调压力")
    if price and up and price >= up * 0.98:
        risks.append("股价贴近布林上轨，上方压力较大")
    if price and ma20 and price < ma20:
        risks.append("股价位于 MA20 之下，中期趋势偏弱")
    if sc["tech_score"] < 5:
        risks.append("技术面整体偏弱，缺乏做多信号")
    vr = snap.get("vol_ratio_5")
    if vr is not None and vr < 0.8:
        risks.append(f"成交清淡（5日量比 {_fmt(vr)}），上涨缺乏量能配合")
    if not risks:
        risks.append("现有数据未识别出显著风险项；仍需关注宏观环境与行业波动")
    risks.append("以上仅为数据层面的观察，**未涵盖**政策、诉讼、大股东减持、突发事件等消息面风险")
    for i, r in enumerate(risks, 1):
        out.append(f"{i}. {r}")
    out.append("")

    # ---------- 四、历史回测 ----------
    out.append("## 四、历史回测（同标的、同区间）")
    out.append("")
    if len(bars) >= 60:
        try:
            # 本金必须买得起至少一手，否则 A股整手规则会让回测全程空仓、
            # 所有策略都显示 0.00% —— 看起来像"策略无效"，实际是本金不够。
            # 这里按「够买约 10 手」自动设定，并取整到万元。
            px = q.get("price") or (bars[-1].get("close") if bars else 0) or 0
            lot_cost = px * 100
            bt_cash = 100000.0
            if lot_cost > 0:
                need = lot_cost * 10
                bt_cash = max(100000.0, math.ceil(need / 10000) * 10000)
            cmp_res = bt_svc.compare_strategies(bars, bt_cash, sym)
            ok = [x for x in cmp_res if "error" not in x]
            if ok:
                out.append("| 策略 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 交易次数 |")
                out.append("|---|---|---|---|---|---|---|")
                for x in ok[:5]:
                    out.append(
                        f"| {x['name']} | {x['total_return']}% | {x['annual_return']}% | "
                        f"{x['max_drawdown']}% | {x['sharpe']} | {x['win_rate']}% | {x['trade_count']} |"
                    )
                best = ok[0]
                bench = best.get("benchmark_return")
                out.append("")
                out.append(f"初始资金 {bt_cash:,.0f} 元"
                           f"（按一手约 {lot_cost:,.0f} 元自动设定，确保能买入整手）。")
                out.append(f"同期买入持有基准收益 **{_fmt(bench)}%**；"
                           f"表现最好的策略是「{best['name']}」（{best['total_return']}%）。")
                out.append("")
                out.append("> 回测已计入佣金（万2.5，最低5元）、印花税（卖出千0.5）、"
                           "过户费与滑点，并遵守 T+1 与涨跌停限制。"
                           "**历史表现不代表未来收益**，且未考虑流动性冲击与停牌。")
        except Exception as exc:  # noqa: BLE001
            out.append(f"_回测未能完成：{exc}_")
    else:
        out.append(f"_K线仅 {len(bars)} 根，不足以回测（需 ≥60 根）_")
    out.append("")

    # ---------- 五、总结 ----------
    out.append("## 五、总结")
    out.append("")
    strong = [lab for lab, scv, _ in sc["tech_items"] if scv >= 7.5][:3]
    weak = [lab for lab, scv, _ in sc["tech_items"] if scv <= 3.5][:3]
    out.append(f"{name} 综合评分 **{sc['score']}/10**，结论 **【{sc['verdict']}】**。"
               f"{('，'.join(bits))}。")
    if strong:
        out.append(f"- 有利因素：{'；'.join(strong)}")
    if weak:
        out.append(f"- 不利因素：{'；'.join(weak)}")
    out.append(f"- {closing}")
    out.append("")
    out.append("---")
    out.append("*本面板由本地规则引擎基于公开行情与财务数据自动生成，所有数值可回溯核对，"
               "不含任何消息面信息。**不构成投资建议**，据此操作风险自负。*")
    out.append("*配置 `SL_AI_API_KEY` 后可启用大模型做更深入的解读（含定性分析）。*")
    return "\n".join(out)


def _call_llm(prompt: str) -> str:
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.ai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": "你是一位严谨专业的证券分析师，只基于给定数据作答。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    with httpx.Client(timeout=settings.ai_timeout, trust_env=False) as c:
        r = c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def generate_report(symbol: str, save: bool = True) -> dict[str, Any]:
    """生成研究报告。"""
    ctx = gather_context(symbol)
    name = ((ctx.get("quote") or {}).get("name")) or symbol
    used_ai = False
    model = "local-rule-engine"
    content = ""

    if settings.ai_enabled and settings.ai_api_key:
        try:
            content = _call_llm(build_prompt(ctx))
            if content:
                used_ai = True
                model = settings.ai_model
        except Exception as exc:  # noqa: BLE001
            log.warning("AI 调用失败，降级本地引擎: %s", exc)

    if not content:
        content = _local_report(ctx)

    if save:
        try:
            db.execute(
                "INSERT INTO ai_reports(symbol,name,kind,content,model) VALUES(?,?,?,?,?)",
                (symbol, name, "single", content, model),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("报告保存失败: %s", exc)

    return {
        "symbol": symbol,
        "name": name,
        "content": content,
        "used_ai": used_ai,
        "model": model,
        "quote": ctx.get("quote"),
        "tech": ctx.get("tech"),
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    }


def market_overview_report() -> str:
    """大盘/自选整体简报。"""
    from . import quote as quote_svc

    idx = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"]
    qs = market.get_quotes(idx)
    out = ["## 大盘概况", ""]
    out.append("| 指数 | 最新 | 涨跌幅 |")
    out.append("|---|---|---|")
    for s in idx:
        q = qs.get(s) or {}
        out.append(f"| {q.get('name') or s} | {_fmt(q.get('price'))} | {_fmt(q.get('pct_change'))}% |")

    try:
        from . import screener as sc
        up_rows = market.snapshot_rows("pct_change > 0", (), "pct_change DESC", 10)
        down_rows = market.snapshot_rows("pct_change < 0", (), "pct_change ASC", 10)
        amt_rows = market.snapshot_rows("", (), "amount DESC", 10)
        stat = db.query_one(
            "SELECT SUM(CASE WHEN pct_change>0 THEN 1 ELSE 0 END) AS up, "
            "SUM(CASE WHEN pct_change<0 THEN 1 ELSE 0 END) AS down, COUNT(*) AS total "
            "FROM market_snapshot WHERE asset_type='stock'"
        )
        if stat and stat["total"]:
            out += ["", "## 市场情绪", ""]
            out.append(f"- 上涨 {stat['up']} 家 / 下跌 {stat['down']} 家（共 {stat['total']} 只）")
            ratio = (stat["up"] or 0) / max(1, stat["total"])
            mood = "偏强" if ratio > 0.6 else ("偏弱" if ratio < 0.4 else "中性")
            out.append(f"- 赚钱效应：**{mood}**（上涨占比 {ratio * 100:.1f}%）")
            out += ["", "## 涨幅榜 Top10", "", "| 代码 | 名称 | 最新 | 涨跌幅 |", "|---|---|---|---|"]
            for r in up_rows:
                out.append(f"| {r['symbol']} | {r['name']} | {_fmt(r['price'])} | {_fmt(r['pct_change'])}% |")
            out += ["", "## 跌幅榜 Top10", "", "| 代码 | 名称 | 最新 | 涨跌幅 |", "|---|---|---|---|"]
            for r in down_rows:
                out.append(f"| {r['symbol']} | {r['name']} | {_fmt(r['price'])} | {_fmt(r['pct_change'])}% |")
            out += ["", "## 成交额 Top10", "", "| 代码 | 名称 | 成交额 | 涨跌幅 |", "|---|---|---|---|"]
            for r in amt_rows:
                out.append(f"| {r['symbol']} | {r['name']} | {_fmt(r['amount'], unit='元')} | {_fmt(r['pct_change'])}% |")
        else:
            out += ["", "_全市场快照尚未建立，请先在「设置」页点击「刷新全市场数据」。_"]
    except Exception as exc:  # noqa: BLE001
        out.append(f"\n_市场统计失败: {exc}_")

    out += ["", "---", "*行情数据来自公开接口，仅供参考，不构成投资建议。*"]
    return "\n".join(out)


def list_reports(limit: int = 30) -> list[dict]:
    return db.rows_to_dicts(
        db.query(
            "SELECT id,symbol,name,kind,model,created_at FROM ai_reports ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )


def get_report(rid: int) -> dict | None:
    row = db.query_one("SELECT * FROM ai_reports WHERE id=?", (rid,))
    return dict(row) if row else None
