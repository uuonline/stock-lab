"""东方财富数据源（主源）。

覆盖：实时行情、日/周/月/分钟 K线、分时、全市场列表、财务指标。
注意 ut 参数必须携带，否则 kline 接口返回 rc=102。
"""
from __future__ import annotations

import time
from typing import Any

from ..config import settings
from ..symbols import SymbolError, from_eastmoney_secid, normalize, to_eastmoney
from .base import (
    FetchError, HostPool, cache, fetch_json, fetch_json_rotating, to_float,
)

import logging as _logging
_logger = _logging.getLogger("stocklab.eastmoney")

UT = "fa5fd1943c7b386f172d6893dbfba10b"
HEADERS = {"Referer": "https://quote.eastmoney.com/"}

# 主机池：主域名被限流时自动切到别的域名族
#
# 实测结论（重要）：东财的封禁按**域名族**生效，不是整站。
#   push2.eastmoney.com 及全部编号分片        → 长时间连接被直接断开
#   push2delay.eastmoney.com 及全部编号分片   → 同时完全正常
# 因此主机池必须横跨两个域名族，否则被封时东财通道会全灭。
_PUSH2_SHARDS = [1, 7, 13, 20, 40, 60, 82, 92, 96, 100, 110]
_PUSH2HIS_SHARDS = [1, 2, 3, 5, 7, 9, 13, 20, 40, 60, 82, 92]

# 行情 + 全市场列表：两个域名族都放进去，delay 族优先
PUSH2_POOL = HostPool(
    [f"{i}.push2delay.eastmoney.com" for i in _PUSH2_SHARDS]
    + ["push2delay.eastmoney.com"]
    + [f"{i}.push2.eastmoney.com" for i in _PUSH2_SHARDS]
    + ["push2.eastmoney.com"],
    cooldown=150.0,
)

# K线：push2his 没有 delay 域名族（实测 push2hisdelay 返回 302，不存在），
# 所以东财 K线被封期间只能靠腾讯兜底 —— 实测腾讯 K线 100% 可用，够用。
PUSH2HIS_POOL = HostPool(
    [f"{i}.push2his.eastmoney.com" for i in _PUSH2HIS_SHARDS] + ["push2his.eastmoney.com"],
    cooldown=150.0,
)

# 分时（trends2）：接口挂在 push2his 的路径上，但实测 **push2delay 域名也能提供**，
# 且 push2his 整族被封时 push2delay 依然正常。所以分时单独用一个池：
# 先试 push2delay（已验证可用），再回落 push2his。
TRENDS_POOL = HostPool(
    [f"{i}.push2delay.eastmoney.com" for i in _PUSH2_SHARDS]
    + ["push2delay.eastmoney.com"]
    + [f"{i}.push2his.eastmoney.com" for i in _PUSH2HIS_SHARDS]
    + ["push2his.eastmoney.com"],
    cooldown=150.0,
)

QUOTE_FIELDS = (
    "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,"
    "f20,f21,f23,f62,f115,f184,f152"
)

# 复权: 0 不复权 1 前复权 2 后复权
PERIOD_MAP = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
    "day": 101, "week": 102, "month": 103,
}

# 全市场列表分组
MARKET_FS = {
    "a_share": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "sh": "m:1+t:2,m:1+t:23",
    "sz": "m:0+t:6,m:0+t:80",
    "bj": "m:0+t:81+s:2048",
    "hk": "m:116+t:3,m:116+t:4",
    "etf": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
    "index": "m:1+s:2,m:0+t:5",
}

LIST_FIELDS = (
    "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f115,f62,f184"
)


def quote_from_diff(row: dict) -> dict:
    code = str(row.get("f12") or "")
    return {
        "code": code,
        "name": row.get("f14") or "",
        "price": to_float(row.get("f2")),
        "pct_change": to_float(row.get("f3")),
        "change": to_float(row.get("f4")),
        "volume": to_float(row.get("f5")),          # 手
        "amount": to_float(row.get("f6")),          # 元
        "amplitude": to_float(row.get("f7")),
        "turnover_rate": to_float(row.get("f8")),
        "pe": to_float(row.get("f9")),
        "vol_ratio": to_float(row.get("f10")),
        "high": to_float(row.get("f15")),
        "low": to_float(row.get("f16")),
        "open": to_float(row.get("f17")),
        "prev_close": to_float(row.get("f18")),
        "market_cap": to_float(row.get("f20")),
        "float_cap": to_float(row.get("f21")),
        "pb": to_float(row.get("f23")),
        "main_net_inflow": to_float(row.get("f62")),
        "pe_ttm": to_float(row.get("f115")),
        "main_net_pct": to_float(row.get("f184")),
    }


def quotes(symbols: list[str], retries: int | None = None,
           timeout: float | None = None, deadline: float | None = None) -> dict[str, dict]:
    """批量实时行情。返回 {symbol: quote}。retries 用于字段补充时的快速模式。"""
    if not symbols:
        return {}
    out: dict[str, dict] = {}
    # 单次最多 50 个，避免 URL 过长
    for i in range(0, len(symbols), 50):
        chunk = symbols[i : i + 50]
        secids = ",".join(to_eastmoney(s) for s in chunk)
        data = fetch_json_rotating(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            PUSH2_POOL,
            params={
                "fltt": 2, "invt": 2, "ut": UT,
                "secids": secids, "fields": QUOTE_FIELDS,
            },
            headers=HEADERS,
            retries=retries,
            timeout=timeout,
            deadline=deadline,
            cache_ttl=settings.quote_cache_ttl,
        )
        diff = (data.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):  # 某些情况下返回 dict
            diff = list(diff.values())
        code_to_symbol = {s.rpartition(".")[0].lstrip("0") or "0": s for s in chunk}
        for row in diff:
            q = quote_from_diff(row)
            code = q["code"]
            sym = code_to_symbol.get(code.lstrip("0") or "0")
            if sym is None:
                # 港股 5 位/指数等，做一次宽松匹配
                for s in chunk:
                    if s.rpartition(".")[0].lstrip("0") == code.lstrip("0"):
                        sym = s
                        break
            if sym is None:
                continue
            q["symbol"] = sym
            out[sym] = q
    return out


def kline(
    symbol: str,
    period: str = "day",
    limit: int = 320,
    adjust: int = 1,
    start: str | None = None,
    end: str | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """K线。period: 1m/5m/15m/30m/60m/day/week/month"""
    klt = PERIOD_MAP.get(period, 101)
    params: dict[str, Any] = {
        "secid": to_eastmoney(symbol),
        "ut": UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": adjust,
        "end": end or "20500101",
        "lmt": max(1, min(limit, 10000)),
    }
    if start:
        params["beg"] = start.replace("-", "")
    else:
        params["beg"] = "0"

    data = fetch_json_rotating(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        PUSH2HIS_POOL,
        params=params,
        headers=HEADERS,
        deadline=deadline,
        cache_ttl=settings.kline_cache_ttl,
    )
    node = data.get("data") or {}
    raw = node.get("klines") or []
    bars: list[dict] = []
    for line in raw:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        bars.append(
            {
                "date": parts[0],
                "open": to_float(parts[1]),
                "close": to_float(parts[2]),
                "high": to_float(parts[3]),
                "low": to_float(parts[4]),
                "volume": to_float(parts[5]),
                "amount": to_float(parts[6]) if len(parts) > 6 else None,
                "amplitude": to_float(parts[7]) if len(parts) > 7 else None,
                "pct_change": to_float(parts[8]) if len(parts) > 8 else None,
                "change": to_float(parts[9]) if len(parts) > 9 else None,
                "turnover_rate": to_float(parts[10]) if len(parts) > 10 else None,
            }
        )
    if limit and len(bars) > limit:
        bars = bars[-limit:]
    return bars


def trends(symbol: str, ndays: int = 1) -> list[dict]:
    """分时数据。"""
    data = fetch_json_rotating(
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
        TRENDS_POOL,
        params={
            "secid": to_eastmoney(symbol), "ut": UT,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ndays": ndays, "iscr": 0, "iscca": 0,
        },
        headers=HEADERS,
        cache_ttl=30,
    )
    node = data.get("data") or {}
    result = []
    # f51=时间 f52=开 f53=收 f54=高 f55=低 f56=量 f57=额 f58=均价
    for line in node.get("trends") or []:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        result.append(
            {
                "time": p[0],
                "open": to_float(p[1]),
                "price": to_float(p[2]),
                "high": to_float(p[3]),
                "low": to_float(p[4]),
                "volume": to_float(p[5]),
                "amount": to_float(p[6]) if len(p) > 6 else None,
                "avg": to_float(p[7]) if len(p) > 7 else None,
            }
        )
    return result


def market_list(
    kind: str = "a_share",
    page: int = 1,
    size: int = 100,
    sort_field: str = "f3",
    ascending: bool = False,
    deadline: float | None = None,
) -> tuple[list[dict], int]:
    """全市场列表。返回 (rows, total)。"""
    # deadline: 可选的墙钟死线，用于诊断类调用（自检）避免被限流的源拖住
    fs = MARKET_FS.get(kind, MARKET_FS["a_share"])
    data = fetch_json_rotating(
        "https://push2.eastmoney.com/api/qt/clist/get",
        PUSH2_POOL,
        params={
            "pn": page, "pz": min(size, 100), "po": 0 if ascending else 1,
            "np": 1, "ut": UT, "fltt": 2, "invt": 2,
            "fid": sort_field, "fs": fs, "fields": LIST_FIELDS,
        },
        headers=HEADERS,
        deadline=deadline,
        cache_ttl=settings.snapshot_cache_ttl,
    )
    node = data.get("data") or {}
    diff = node.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    rows = []
    for row in diff:
        q = quote_from_diff(row)
        code = q["code"]
        mkt = {
            "a_share": None, "sh": "SH", "sz": "SZ", "bj": "BJ",
            "hk": "HK", "etf": None, "index": None,
        }.get(kind)
        try:
            if mkt:
                sym = normalize(code, default_market=mkt)
            else:
                sym = normalize(code)
        except SymbolError:
            # 代码格式不认识（如港股的权证/临时代码），跳过是正常的
            continue
        except Exception as exc:  # noqa: BLE001
            # 其他异常属于代码问题，必须暴露出来
            _logger.error("列表标的解析异常 code=%s: %s", code, exc, exc_info=True)
            continue
        q["symbol"] = sym
        rows.append(q)
    return rows, int(node.get("total") or 0)


def fundamentals(symbol: str, page_size: int = 12) -> dict:
    """主要财务指标（东财 F10）。"""
    code, _, mkt = symbol.rpartition(".")
    secucode = f"{code}.{mkt}"
    data = fetch_json(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        params={
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": 1,
            "pageSize": page_size,
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        },
        headers={"Referer": "https://emweb.securities.eastmoney.com/"},
        cache_ttl=3600,
    )
    result = data.get("result") or {}
    rows = result.get("data") or []
    if not rows:
        raise FetchError(f"无财务数据: {symbol}")

    def pick(k: str) -> Any:
        return to_float(rows[0].get(k))

    latest = {
        "report_date": str(rows[0].get("REPORT_DATE") or "")[:10],
        "report_name": rows[0].get("REPORT_DATE_NAME") or "",
        "eps": pick("EPSJB"),
        "bps": pick("BPS"),
        "revenue": pick("TOTALOPERATEREVE"),
        "net_profit": pick("PARENTNETPROFIT"),
        "revenue_yoy": pick("TOTALOPERATEREVETZ"),
        "profit_yoy": pick("PARENTNETPROFITTZ"),
        "roe": pick("ROEJQ"),
        "gross_margin": pick("XSMLL"),
        "net_margin": pick("XSJLL"),
        "debt_ratio": pick("ZCFZL"),
        "current_ratio": pick("LD"),
        "quick_ratio": pick("SD"),
        "cash_per_share": pick("MGJYXJJE"),
        "deduct_profit": pick("KCFJCXSYJLR"),
        "deduct_profit_yoy": pick("KCFJCXSYJLRTZ"),
    }
    history = []
    for r in rows:
        history.append(
            {
                "report_date": str(r.get("REPORT_DATE") or "")[:10],
                "revenue": to_float(r.get("TOTALOPERATEREVE")),
                "net_profit": to_float(r.get("PARENTNETPROFIT")),
                "revenue_yoy": to_float(r.get("TOTALOPERATEREVETZ")),
                "profit_yoy": to_float(r.get("PARENTNETPROFITTZ")),
                "roe": to_float(r.get("ROEJQ")),
                "gross_margin": to_float(r.get("XSMLL")),
                "net_margin": to_float(r.get("XSJLL")),
                "debt_ratio": to_float(r.get("ZCFZL")),
                "eps": to_float(r.get("EPSJB")),
                "bps": to_float(r.get("BPS")),
            }
        )
    history.reverse()
    return {"symbol": symbol, "latest": latest, "history": history}


def full_snapshot(
    kind: str = "a_share",
    page_size: int = 100,
    max_pages: int = 200,
    page_retries: int = 3,
    progress: Any = None,
    stats: dict[str, Any] | None = None,
) -> list[dict]:
    """抓取全市场快照（用于选股）。

    重要：东财 clist 单页实际最多返回 100 条，即使 pz 传 200 也只给 100。
    因此页数必须按「首屏实际返回条数」推导，否则会少抓一半数据
    （曾经因此 5560 只只入库 2800 只）。

    分页过程中东财偶发频控返回空 body。策略：单页最多重试 page_retries 次，
    仍失败则跳过该页继续抓后面的页 —— 宁可少几十只，也不要整体失败导致
    数据库长期停留在半截状态。
    """
    # 首页也重试，避免开局就空手而归
    # 关键：必须用**稳定**的排序键分页。
    # 默认按涨跌幅(f3)排序，而盘中涨跌幅时刻在变，同一只股票会在页与页之间
    # 移动 —— 结果是有的股票被翻两次、有的永远翻不到。实测 6 页就重复 3 条，
    # 放大到 56 页约丢 30 只且每次不同。
    # 按代码(f12)升序则完全稳定，实测页与页之间严格连续无重复。
    sort_key = "f12"
    rows: list[dict] = []
    total = 0
    for attempt in range(page_retries):
        try:
            rows, total = market_list(
                kind, page=1, size=page_size, sort_field=sort_key, ascending=True
            )
            if rows:
                break
        except FetchError:
            pass
        time.sleep(0.6 * (attempt + 1))
    if not rows:
        raise FetchError(f"快照首页抓取失败: {kind}")

    all_rows: list[dict] = list(rows)
    # 服务器真实页大小（通常 100），据此外推总页数
    effective = len(rows)
    if progress:
        progress(1, 1, len(all_rows))
    if total <= len(rows):
        return all_rows

    pages = min(max_pages, (total + effective - 1) // effective)
    failed_pages: list[int] = []
    for p in range(2, pages + 1):
        chunk: list[dict] = []
        for attempt in range(page_retries):
            try:
                chunk, _ = market_list(
                    kind, page=p, size=page_size, sort_field=sort_key, ascending=True
                )
                if chunk:
                    break
            except FetchError:
                pass
            time.sleep(0.5 * (attempt + 1))
        if chunk:
            all_rows.extend(chunk)
        else:
            failed_pages.append(p)
        if progress:
            progress(p, pages, len(all_rows))
        time.sleep(0.1)

    # 第一轮结束后，对失败的页做补偿重试。
    # 实测失败通常是瞬时频控，隔几秒再抓基本都能成功；不做这一步的话
    # 用户会拿到「就绪但少了 400 多只」的快照，选股结果就偏了。
    # 频控需要时间消退，所以做多轮、且每轮等待递增。
    if failed_pages:
        import logging
        _log = logging.getLogger("stocklab.eastmoney")
        for round_no in range(2):
            if not failed_pages:
                break
            wait = 2.0 + round_no * 4.0
            _log.info(
                "快照 %s 第 %d 轮补偿重试：%d 页待补，先等待 %.1fs",
                kind, round_no + 1, len(failed_pages), wait,
            )
            time.sleep(wait)
            still_failed: list[int] = []
            for p in failed_pages:
                chunk = []
                for attempt in range(page_retries):
                    try:
                        chunk, _ = market_list(
                            kind, page=p, size=page_size, sort_field=sort_key, ascending=True
                        )
                        if chunk:
                            break
                    except FetchError:
                        pass
                    time.sleep(0.8 * (attempt + 1))
                if chunk:
                    all_rows.extend(chunk)
                else:
                    still_failed.append(p)
                time.sleep(0.2)
            failed_pages = still_failed
            if progress:
                progress(pages, pages, len(all_rows))

    if failed_pages and len(all_rows) < total * 0.6:
        raise FetchError(
            f"快照抓取严重不完整：仅 {len(all_rows)}/{total} 条，失败页 {failed_pages[:10]}"
        )
    if failed_pages:
        import logging
        logging.getLogger("stocklab.eastmoney").warning(
            "快照 %s 补偿重试后仍有 %d 页失败（%s），共 %d/%d 条；"
            "再点一次「刷新全市场快照」可补齐（已入库数据是 UPSERT，不会重复）",
            kind, len(failed_pages), failed_pages[:10], len(all_rows), total,
        )
    if stats is not None:
        stats.update({
            "expected_total": total,
            "fetched": len(all_rows),
            "failed_pages": len(failed_pages),
            "complete": not failed_pages,
        })
    return all_rows
