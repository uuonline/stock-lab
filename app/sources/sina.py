"""新浪行情数据源。

两件事：
  1. 实时行情（第三备源）
  2. **全市场列表**（东财列表接口的兜底）

第 2 点很重要：全市场列表原本只有东财提供，而东财是会整站封 IP 的
（实测被封时所有分片全部返回连接断开）。此时新浪列表接口仍然正常，
返回 5564 只 A股（含北交所），且带 PE / PB / 市值 / 换手率，
足以重建选股所需的快照。
"""
from __future__ import annotations

import json
import logging
import time

from ..symbols import SymbolError, normalize, to_sina
from .base import fetch_text, to_float

log = logging.getLogger("stocklab.sina")

URL = "https://hq.sinajs.cn/list="
HEADERS = {"Referer": "https://finance.sina.com.cn"}

KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
)

# 新浪用「分钟数」表示周期
KLINE_SCALE = {
    "5m": 5, "15m": 15, "30m": 30, "60m": 60,
    "day": 240, "week": 1200, "month": 7200,
}

LIST_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
COUNT_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)

# 新浪的 node 分类；港股/指数不在该接口内
LIST_NODES: dict[str, str | None] = {
    "a_share": "hs_a",
    "sh": "sh_a",
    "sz": "sz_a",
    "bj": "bj_a",
    "etf": "etf_hq_fund",
    "hk": None,
    "index": None,
}


def quotes(symbols: list[str], retries: int | None = None,
           timeout: float | None = None, deadline: float | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not symbols:
        return out
    for i in range(0, len(symbols), 60):
        chunk = symbols[i : i + 60]
        codes = ",".join(to_sina(s) for s in chunk)
        text = fetch_text(URL + codes, headers=HEADERS, encoding="gbk", retries=retries,
                          timeout=timeout, cache_ttl=5)
        for line in text.splitlines():
            line = line.strip()
            if "hq_str_" not in line or "=" not in line:
                continue
            head, _, payload = line.partition("=")
            key = head.split("hq_str_")[-1].strip()
            body = payload.strip().strip('";')
            if not body:
                continue
            p = body.split(",")
            if len(p) < 10:
                continue
            symbol = None
            for s in chunk:
                if to_sina(s) == key:
                    symbol = s
                    break
            if symbol is None:
                continue
            price = to_float(p[3])
            prev = to_float(p[2])
            change = None
            pct = None
            if price is not None and prev:
                change = round(price - prev, 4)
                pct = round((price - prev) / prev * 100, 4)
            volume = to_float(p[8])          # 股
            out[symbol] = {
                "symbol": symbol,
                "code": key[2:],
                "name": p[0],
                "open": to_float(p[1]),
                "prev_close": prev,
                "price": price,
                "high": to_float(p[4]),
                "low": to_float(p[5]),
                "volume": None if volume is None else volume / 100.0,   # 股 -> 手
                "amount": to_float(p[9]),
                "change": change,
                "pct_change": pct,
                "update_time": f"{p[30]} {p[31]}" if len(p) > 31 else "",
                "_source": "sina",
            }
    return out


def list_total(kind: str = "a_share") -> int:
    """新浪该分类下的标的总数。"""
    node = LIST_NODES.get(kind)
    if not node:
        return 0
    try:
        text = fetch_text(COUNT_URL, params={"node": node}, headers=HEADERS, cache_ttl=300)
        return int(to_float(text.strip().strip('"')) or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("新浪列表总数获取失败 %s: %s", kind, exc)
        return 0


def market_list_page(kind: str = "a_share", page: int = 1, size: int = 100) -> list[dict]:
    """新浪全市场列表的一页，返回结构与东财 market_list 对齐。"""
    node = LIST_NODES.get(kind)
    if not node:
        return []
    text = fetch_text(
        LIST_URL,
        params={
            "page": page, "num": min(size, 100), "sort": "symbol",
            "asc": 1, "node": node, "symbol": "", "_s_r_a": "page",
        },
        headers=HEADERS,
        cache_ttl=60,
    )
    text = text.strip()
    if not text or text in ("null", "[]"):
        return []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"新浪列表 JSON 解析失败: {text[:120]}") from exc
    if not isinstance(raw, list):
        return []

    rows: list[dict] = []
    for item in raw:
        sym_raw = str(item.get("symbol") or "")
        code = str(item.get("code") or "")
        if not code:
            continue
        # symbol 形如 sh600519 / sz000001 / bj920000
        mkt = None
        if len(sym_raw) > 2:
            mkt = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym_raw[:2].lower())
        try:
            sym = normalize(code, default_market=mkt) if mkt else normalize(code)
        except SymbolError:
            continue
        vol = to_float(item.get("volume"))        # 股
        mktcap = to_float(item.get("mktcap"))     # 万元
        nmc = to_float(item.get("nmc"))           # 万元
        per = to_float(item.get("per"))
        rows.append({
            "symbol": sym,
            "code": code,
            "name": item.get("name") or "",
            "price": to_float(item.get("trade")),
            "change": to_float(item.get("pricechange")),
            "pct_change": to_float(item.get("changepercent")),
            "prev_close": to_float(item.get("settlement")),
            "open": to_float(item.get("open")),
            "high": to_float(item.get("high")),
            "low": to_float(item.get("low")),
            "volume": None if vol is None else vol / 100.0,     # 股 -> 手
            "amount": to_float(item.get("amount")),
            "turnover_rate": to_float(item.get("turnoverratio")),
            # 新浪的 per 为负表示亏损，转成 None 更符合选股语义
            "pe": per if (per is not None and per > 0) else None,
            "pe_ttm": per if (per is not None and per > 0) else None,
            "pb": to_float(item.get("pb")),
            "market_cap": None if mktcap is None else mktcap * 10000.0,   # 万元 -> 元
            "float_cap": None if nmc is None else nmc * 10000.0,
            "amplitude": None,
            "_source": "sina",
        })
    return rows


def full_market_list(
    kind: str = "a_share",
    page_size: int = 100,
    max_pages: int = 120,
    page_retries: int = 2,
    progress=None,
    stats: dict | None = None,
) -> list[dict]:
    """抓取新浪全市场列表（东财列表的兜底路径）。

    与东财一致地按代码升序分页（稳定键），逐页重试，失败页最后补偿一轮。
    """
    total = list_total(kind)
    if not total:
        # 拿不到总数时按最大页数试探，直到某页为空
        total = page_size * max_pages

    all_rows: list[dict] = []
    failed_pages: list[int] = []
    pages = min(max_pages, (total + page_size - 1) // page_size)

    for p in range(1, pages + 1):
        chunk: list[dict] = []
        for attempt in range(page_retries):
            try:
                chunk = market_list_page(kind, p, page_size)
                if chunk:
                    break
            except Exception as exc:  # noqa: BLE001
                log.debug("新浪列表第 %d 页失败: %s", p, exc)
            time.sleep(0.3 * (attempt + 1))
        if chunk:
            all_rows.extend(chunk)
        else:
            failed_pages.append(p)
        if progress:
            progress(p, pages, len(all_rows))
        time.sleep(0.05)

    # 补偿一轮
    if failed_pages:
        time.sleep(1.5)
        still: list[int] = []
        for p in failed_pages:
            try:
                chunk = market_list_page(kind, p, page_size)
            except Exception:  # noqa: BLE001
                chunk = []
            if chunk:
                all_rows.extend(chunk)
            else:
                still.append(p)
            time.sleep(0.1)
        if still:
            log.warning("新浪列表仍有 %d 页失败: %s", len(still), still[:10])

    if not all_rows:
        raise ValueError(f"新浪列表抓取失败: {kind}")
    if stats is not None:
        stats.update({
            "source": "sina",
            "expected_total": total,
            "fetched": len(all_rows),
            "failed_pages": len(failed_pages),
            "complete": not failed_pages,
        })
    return all_rows



def kline(symbol: str, period: str = "day", limit: int = 320,
          retries: int | None = None, timeout: float | None = None) -> list[dict]:
    """新浪 K线（第三备源）。

    只覆盖 A股 / 指数 / ETF —— 港股返回 null（实测 hk00700 → null），
    港股仍需东财或腾讯。字段里没有成交额，只有成交量（股）。
    """
    scale = KLINE_SCALE.get(period)
    if scale is None:
        raise ValueError(f"新浪不支持该周期: {period}")
    if symbol.upper().endswith(".HK"):
        raise ValueError(f"新浪 K线不支持港股: {symbol}")

    text = fetch_text(
        KLINE_URL,
        params={"symbol": to_sina(symbol), "scale": scale, "ma": "no",
                "datalen": max(1, min(limit, 1023))},
        headers=HEADERS,
        retries=retries,
        timeout=timeout,
        cache_ttl=300,
    )
    text = text.strip()
    if not text or text == "null":
        raise ValueError(f"新浪无K线数据: {symbol} {period}")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"新浪K线解析失败: {text[:120]}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"新浪K线返回异常结构: {type(raw).__name__}")

    bars: list[dict] = []
    for item in raw:
        day = str(item.get("day") or "")
        if not day:
            continue
        vol = to_float(item.get("volume"))     # 股
        bars.append({
            "date": day,
            "open": to_float(item.get("open")),
            "close": to_float(item.get("close")),
            "high": to_float(item.get("high")),
            "low": to_float(item.get("low")),
            "volume": None if vol is None else vol / 100.0,   # 股 -> 手
            "amount": None,
        })
    if limit and len(bars) > limit:
        bars = bars[-limit:]
    return bars
