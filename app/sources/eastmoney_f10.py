"""东财 F10 数据源：主营构成、行业对比、股东高管增减持。

这三个数据都在**另一个域名族**上（`emweb.securities.eastmoney.com` 与
`datacenter-web.eastmoney.com`），和行情用的 `push2*` 是两回事 ——
实测 `push2` 整族被限流时，这几个接口仍然正常返回。

> 为什么单独一个模块：这些是「公告类/档案类」数据，变动很慢
> （主营构成按报告期、增减持按公告），所以缓存时间可以放很长，
> 和行情那套分钟级缓存完全不是一回事，混在一起会互相干扰。

数据都是**结构化的原始披露**，不是我们推断出来的：
  · 主营构成   —— 公司按产品/地区口径披露的收入拆分，带收入、占比、毛利率
  · 经营评述   —— 公司自己在定期报告里写的业务描述（原文）
  · 行业对比   —— 同行公司名单 + 行业平均/中值的 PE、ROE 等
  · 增减持     —— 逐笔的高管/股东买卖记录，带数量、均价、原因
"""
from __future__ import annotations

import logging
from typing import Any

from .base import FetchError, fetch_json

log = logging.getLogger("stocklab.f10")

# 档案类数据变动很慢，缓存 6 小时
TTL = 21600

F10_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/{page}/PageAjax"
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

HEADERS = {
    "Referer": "https://emweb.securities.eastmoney.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
}

# 主营构成的口径编号
MAINOP_TYPES = {"1": "按行业", "2": "按产品", "3": "按地区"}


def _f10_code(symbol: str) -> str | None:
    """002241.SZ → SZ002241。港股/ETF 不支持（F10 只覆盖 A股）。"""
    s = symbol.upper()
    if s.endswith(".SZ"):
        return "SZ" + s[:-3]
    if s.endswith(".SH"):
        return "SH" + s[:-3]
    if s.endswith(".BJ"):
        return "BJ" + s[:-3]
    return None


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _f10(page: str, symbol: str) -> dict[str, Any]:
    code = _f10_code(symbol)
    if not code:
        raise FetchError(f"F10 不支持该市场: {symbol}")
    return fetch_json(
        F10_URL.format(page=page), params={"code": code},
        headers=HEADERS, timeout=15, cache_ttl=TTL,
    )


def _dc(report: str, symbol: str, extra: dict[str, Any] | None = None) -> list[dict]:
    code = _f10_code(symbol)
    if not code:
        raise FetchError(f"数据中心不支持该市场: {symbol}")
    params = {
        "reportName": report,
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code[2:]}")',
        "pageSize": "30",
        "source": "WEB",
        "client": "WEB",
    }
    params.update(extra or {})
    d = fetch_json(DC_URL, params=params, headers=HEADERS, timeout=15, cache_ttl=TTL)
    return ((d.get("result") or {}).get("data")) or []


# ---------------- 主营构成 ----------------

def business(symbol: str) -> dict[str, Any]:
    """主营构成 + 经营范围 + 经营评述（公司自述）。"""
    out: dict[str, Any] = {"ok": False, "segments": {}, "scope": None, "review": None,
                           "report_name": None}
    try:
        d = _f10("BusinessAnalysis", symbol)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"主营构成获取失败: {exc}"
        return out

    rows = d.get("zygcfx") or []
    if isinstance(rows, dict):
        rows = [rows]
    # 只取最新一期报告，否则会把历年数据混在一起
    latest = None
    for r in rows:
        rd = str(r.get("REPORT_DATE") or "")
        if rd and (latest is None or rd > latest):
            latest = rd
    seg: dict[str, list[dict]] = {}
    for r in rows:
        if latest and str(r.get("REPORT_DATE") or "") != latest:
            continue
        kind = MAINOP_TYPES.get(str(r.get("MAINOP_TYPE")), "其他")
        item = {
            "name": r.get("ITEM_NAME"),
            "income": _num(r.get("MAIN_BUSINESS_INCOME")),
            "ratio": _num(r.get("MBI_RATIO")),
            # 按地区口径东财不给毛利率（返回 0），0 要当作"未披露"而不是 0%
            "gross_margin": (_num(r.get("GROSS_RPOFIT_RATIO")) or None),
        }
        seg.setdefault(kind, []).append(item)
    for k in seg:
        seg[k].sort(key=lambda x: -(x["income"] or 0))

    scope = None
    zyfw = d.get("zyfw") or []
    if isinstance(zyfw, list) and zyfw:
        scope = (zyfw[0] or {}).get("BUSINESS_SCOPE")
    review = None
    jyps = d.get("jyps") or []
    if isinstance(jyps, list) and jyps:
        review = (jyps[0] or {}).get("BUSINESS_REVIEW")

    out.update({
        "ok": bool(seg or scope or review),
        "segments": seg,
        "scope": scope,
        "review": review,
        "report_name": latest[:10] if latest else None,
        "as_of": latest[:10] if latest else None,
    })
    return out


# ---------------- 行业对比 ----------------

def industry(symbol: str) -> dict[str, Any]:
    """同行对比。

    ⚠️ 东财给的**不是一份同行名单，而是三份榜单**，各自包含不同的公司：

      czxbj  成长性比较：营收/利润增速 + 行业排名
      gzbj   估值比较：PE / PB / PEG，含"行业平均"行
      dbfxbj 财务对比：ROE / 净利率 / 周转 / 权益乘数，含"行业中值"行

    实测歌尔股份这三张表分别是 5 家、5 家、5 家，**互相之间只有一两家重叠**
    （协创数据在三张表里都有，其余各不相同）。所以不能强行合并成一份名单 ——
    合并后每家只有自己那张表的指标，其余全是空，看起来像数据缺失，
    实际是"这家公司只在这一个维度上被列为对比对象"。
    """
    out: dict[str, Any] = {"ok": False, "growth": [], "valuation": [],
                           "finance": [], "avg": {}, "median": {}, "scale": None}
    try:
        d = _f10("IndustryAnalysis", symbol)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"行业数据获取失败: {exc}"
        return out

    def name_of(r: dict) -> str | None:
        return r.get("CORRE_SECURITY_NAME")

    for r in (d.get("czxbj") or []):
        nm = name_of(r)
        if not nm or nm in ("行业平均", "行业中值", "本公司"):
            continue
        out["growth"].append({
            "name": nm, "code": r.get("CORRE_SECURITY_CODE"),
            "revenue_yoy": _num(r.get("YYSRTB")), "profit_yoy": _num(r.get("JLRTB")),
            "revenue_3y": _num(r.get("YYSR_3Y")), "profit_3y": _num(r.get("JLR_3Y")),
            "rank": _num(r.get("PAIMING")),
        })
    for r in (d.get("gzbj") or []):
        nm = name_of(r)
        if not nm:
            continue
        if nm == "行业平均":
            out["avg"] = {"pe_ttm": _num(r.get("PE_TTM")), "pb": _num(r.get("PB")),
                          "peg": _num(r.get("PEG"))}
            continue
        if nm == "行业中值":
            continue
        out["valuation"].append({
            "name": nm, "code": r.get("CORRE_SECURITY_CODE"),
            "pe_ttm": _num(r.get("PE_TTM")), "pb": _num(r.get("PB")),
            "peg": _num(r.get("PEG")),
        })
    for r in (d.get("dbfxbj") or []):
        nm = name_of(r)
        if not nm:
            continue
        if nm == "行业中值":
            out["median"] = {"roe_avg": _num(r.get("ROE_AVG")),
                             "net_margin": _num(r.get("XSJLL_AVG"))}
            continue
        if nm == "行业平均":
            continue
        out["finance"].append({
            "name": nm, "code": r.get("CORRE_SECURITY_CODE"),
            "roe_avg": _num(r.get("ROE_AVG")), "net_margin": _num(r.get("XSJLL_AVG")),
            "debt_ratio": _num(r.get("QYCS_AVG")),
        })

    g = (d.get("gsgm") or [{}])[0]
    if g:
        out["scale"] = {
            "market_cap": _num(g.get("TOTAL_CAP")),
            "market_cap_rank": _num(g.get("TOTAL_CAP_RANK")),
            "revenue": _num(g.get("TOTAL_OPERATEINCOME")),
            "revenue_rank": _num(g.get("TOTAL_OPERATEINCOME_RANK")),
            "net_profit": _num(g.get("NETPROFIT")),
            "net_profit_rank": _num(g.get("NETPROFIT_RANK")),
        }
    # 三张榜单共用同一个报告期，取出来告诉用户数据有多新 ——
    # 实测这个日期是**年报口径**（2025-12-31），比当前时间滞后大半年，
    # 不标出来用户会以为看到的是最新同业数据。
    dates = sorted({str(r.get("REPORT_DATE") or "")[:10]
                    for k in ("czxbj", "gzbj", "dbfxbj")
                    for r in (d.get(k) or []) if r.get("REPORT_DATE")})
    out["as_of"] = dates[-1] if dates else None
    out["ok"] = bool(out["growth"] or out["valuation"] or out["finance"])
    return out


# ---------------- 股东与高管增减持 ----------------

def holder_changes(symbol: str, days: int = 365) -> dict[str, Any]:
    """近一年高管与股东增减持。

    ⚠️ 单位陷阱：`CHANGE_NUM` 是**万股**，不是股。实测
    「歌尔集团有限公司 CHANGE_NUM=356.84」对应的公告是减持 356.84 万股。
    直接用会把规模算错一万倍，所以这里统一换算成股。
    """
    out: dict[str, Any] = {"ok": False, "executives": [], "holders": [],
                           "exec_net_shares": 0.0, "holder_net_shares": 0.0,
                           "direction": None}
    errs = []
    try:
        ex = _dc("RPT_EXECUTIVE_HOLD_DETAILS", symbol,
                 {"sortColumns": "CHANGE_DATE", "sortTypes": "-1"})
    except Exception as exc:  # noqa: BLE001
        ex, _ = [], errs.append(str(exc))
    try:
        hd = _dc("RPT_SHARE_HOLDER_INCREASE", symbol,
                 {"sortColumns": "NOTICE_DATE", "sortTypes": "-1"})
    except Exception as exc:  # noqa: BLE001
        hd, _ = [], errs.append(str(exc))

    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    net_exec = net_holder = 0.0
    for r in ex:
        d0 = str(r.get("CHANGE_DATE") or "")[:10]
        shares = _num(r.get("CHANGE_SHARES"))
        if shares is None:
            continue
        # 高管表给的是股（实测 CHANGE_SHARES=500、均价 17.762、金额 8881，
        # 500×17.762=8881 完全吻合），正为增持、负为减持
        item = {
            "date": d0, "person": r.get("PERSON_NAME"), "shares": shares,
            "price": _num(r.get("AVERAGE_PRICE")),
            "amount": _num(r.get("CHANGE_AMOUNT")),
            "reason": r.get("CHANGE_REASON"),
        }
        out["executives"].append(item)
        if not d0 or d0 >= cutoff:
            net_exec += shares
            item["in_window"] = True

    for r in hd:
        d0 = str(r.get("NOTICE_DATE") or "")[:10]
        num = _num(r.get("CHANGE_NUM"))          # 单位是**万股**
        if num is None:
            continue
        # 表格自带 DIRECTION 字段（增持/减持），不要用 CHANGE_RATE 的正负去猜 ——
        # 实测该字段口径与持股比例不是一回事，猜会猜反。
        direction = str(r.get("DIRECTION") or "")
        sign = -1 if "减持" in direction else 1
        shares = num * 10000 * sign
        item = {
            "date": d0, "holder": r.get("HOLDER_NAME"), "shares": shares,
            "direction": direction or ("增持" if sign > 0 else "减持"),
            "rate": _num(r.get("CHANGE_RATE")),
            "after_rate": _num(r.get("AFTER_CHANGE_RATE")),
        }
        out["holders"].append(item)
        if not d0 or d0 >= cutoff:
            net_holder += shares
            item["in_window"] = True

    window_exec = [x for x in out["executives"] if x.get("in_window")]
    window_holder = [x for x in out["holders"] if x.get("in_window")]
    out["exec_net_shares"] = net_exec
    out["holder_net_shares"] = net_holder
    out["window_exec_count"] = len(window_exec)
    out["window_holder_count"] = len(window_holder)
    all_dates = [x["date"] for x in out["executives"] + out["holders"] if x.get("date")]
    out["as_of"] = max(all_dates) if all_dates else None
    out["errors"] = errs

    # 取数成功但窗口内没记录，是**有效结论**（近一年确实没公布增减持），
    # 不能和"取数失败"混为一谈 —— 否则界面上会显示成"无数据"，
    # 而事实是"有一段时间没动作了"，这两件事对判断的意义完全不同。
    if errs and not out["executives"] and not out["holders"]:
        out["ok"] = False
        out["reason"] = "；".join(errs)
    else:
        out["ok"] = True
        if window_exec or window_holder:
            total = net_exec + net_holder
            out["direction"] = ("净买入" if total > 0 else
                                ("净卖出" if total < 0 else "持平"))
        else:
            out["direction"] = "无记录"
    return out


def available(symbol: str) -> bool:
    """F10 只覆盖 A股。"""
    return _f10_code(symbol) is not None
