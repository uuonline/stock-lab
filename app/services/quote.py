"""自选股与行情服务。"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .. import db
from ..config import settings
from ..symbols import asset_type, board_of, display_name, normalize
from ..sources import market
from ..sources.base import swallow

log = logging.getLogger("stocklab.quote")

DEFAULT_GROUP = "默认分组"


# ---------------- 自选股 ----------------

def add_watch(symbol: str, group: str = DEFAULT_GROUP, note: str = "") -> dict:
    sym = normalize(symbol)
    q = market.get_quotes([sym]).get(sym) or {}
    name = q.get("name") or display_name(sym)
    order_row = db.query_one(
        "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM watchlist WHERE group_name=?",
        (group,),
    )
    db.execute(
        "INSERT INTO watchlist(group_name,symbol,name,asset_type,note,sort_order) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(group_name,symbol) DO UPDATE SET name=excluded.name, note=excluded.note",
        (group, sym, name, asset_type(sym), note, (order_row or {"n": 1})["n"]),
    )
    return {"symbol": sym, "name": name, "group_name": group}


def remove_watch(symbol: str, group: str | None = None) -> None:
    sym = normalize(symbol)
    if group:
        db.execute("DELETE FROM watchlist WHERE symbol=? AND group_name=?", (sym, group))
    else:
        db.execute("DELETE FROM watchlist WHERE symbol=?", (sym,))


def list_groups() -> list[str]:
    rows = db.query("SELECT DISTINCT group_name FROM watchlist ORDER BY group_name")
    groups = [r["group_name"] for r in rows]
    if DEFAULT_GROUP not in groups:
        groups.insert(0, DEFAULT_GROUP)
    return groups


def create_group(name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("分组名不能为空")
    # 用一条占位记录让空分组也能被列出
    db.kv_set(f"group:{name}", True)


def watchlist_with_quotes(group: str | None = None, with_tech: bool = False) -> list[dict]:
    """自选股 + 实时行情（+ 可选技术评分）。"""
    sql = "SELECT * FROM watchlist"
    params: tuple = ()
    if group:
        sql += " WHERE group_name=?"
        params = (group,)
    sql += " ORDER BY group_name, sort_order, id"
    rows = db.rows_to_dicts(db.query(sql, params))
    if not rows:
        return []

    symbols = [r["symbol"] for r in rows]
    try:
        quotes = market.get_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        log.warning("自选行情获取失败: %s", exc)
        quotes = {}

    out = []
    for r in rows:
        sym = r["symbol"]
        q = quotes.get(sym) or {}
        item = {
            "id": r["id"],
            "symbol": sym,
            "name": q.get("name") or r.get("name") or display_name(sym),
            "group_name": r["group_name"],
            "note": r.get("note") or "",
            "asset_type": r.get("asset_type") or asset_type(sym),
            "board": board_of(sym),
            "price": q.get("price"),
            "pct_change": q.get("pct_change"),
            "change": q.get("change"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "prev_close": q.get("prev_close"),
            "volume": q.get("volume"),
            "amount": q.get("amount"),
            "turnover_rate": q.get("turnover_rate"),
            "vol_ratio": q.get("vol_ratio"),
            "pe_ttm": q.get("pe_ttm") or q.get("pe"),
            "pb": q.get("pb"),
            "market_cap": q.get("market_cap"),
            "amplitude": q.get("amplitude"),
            "main_net_inflow": q.get("main_net_inflow"),
            "source": q.get("_source"),
        }
        out.append(item)

    if with_tech:
        for item in out:
            try:
                bars = market.get_kline(item["symbol"], "day", 130)
                from . import indicators as ta
                snap = ta.latest_snapshot(bars) if bars else {}
                item["tech_score"] = snap.get("score")
                item["tech_rating"] = snap.get("rating")
                item["signals"] = [s["text"] for s in snap.get("signals", [])]
            except Exception:  # noqa: BLE001
                item["tech_score"] = None
                item["tech_rating"] = None
                item["signals"] = []
    return out


def reorder_watch(group: str, symbols: list[str]) -> None:
    for i, sym in enumerate(symbols):
        try:
            db.execute(
                "UPDATE watchlist SET sort_order=? WHERE group_name=? AND symbol=?",
                (i, group, normalize(sym)),
            )
        except SymbolError:
            continue
        except Exception as exc:  # noqa: BLE001
            swallow(exc, log, f"自选排序 {sym}")
            continue


# ---------------- 市场状态 ----------------

def now_cn() -> dt.datetime:
    return dt.datetime.now()


def market_status() -> dict[str, Any]:
    """判断 A股 / 港股 是否开市。"""
    now = now_cn()
    weekday = now.weekday()
    hm = now.hour * 60 + now.minute

    def in_range(ranges: list[tuple[int, int]]) -> bool:
        return any(a <= hm < b for a, b in ranges)

    is_weekday = weekday < 5
    a_am = (9 * 60 + 30, 11 * 60 + 30)
    a_pm = (13 * 60, 15 * 60)
    hk_am = (9 * 60 + 30, 12 * 60)
    hk_pm = (13 * 60, 16 * 60)

    a_open = is_weekday and in_range([a_am, a_pm])
    hk_open = is_weekday and in_range([hk_am, hk_pm])

    if a_open:
        session = "交易中"
    elif is_weekday and hm < a_am[0]:
        session = "未开盘"
    elif is_weekday and a_am[1] <= hm < a_pm[0]:
        session = "午间休市"
    elif is_weekday and hm >= a_pm[1]:
        session = "已收盘"
    else:
        session = "休市"

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": weekday,
        "a_share_open": a_open,
        "hk_open": hk_open,
        "session": session,
        "is_trading_day": is_weekday,
    }


def indices_overview() -> list[dict]:
    """主要指数概览。"""
    symbols = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000905.SH", "000688.SH", "00700.HK"]
    qs = market.get_quotes(symbols)
    out = []
    for s in symbols:
        q = qs.get(s) or {}
        out.append({
            "symbol": s,
            "name": q.get("name") or display_name(s),
            "price": q.get("price"),
            "pct_change": q.get("pct_change"),
            "change": q.get("change"),
            "amount": q.get("amount"),
        })
    return out


def movers(kind: str = "up", limit: int = 10) -> list[dict]:
    """涨跌幅榜。"""
    if kind == "up":
        where, order = "asset_type='stock' AND pct_change IS NOT NULL AND pct_change > 0", "pct_change DESC"
    elif kind == "down":
        where, order = "asset_type='stock' AND pct_change IS NOT NULL AND pct_change < 0", "pct_change ASC"
    elif kind == "amount":
        where, order = "asset_type='stock'", "amount DESC"
    elif kind == "turnover":
        where, order = "asset_type='stock' AND turnover_rate IS NOT NULL", "turnover_rate DESC"
    else:
        where, order = "asset_type='stock'", "amount DESC"
    return market.snapshot_rows(where, (), order, limit)


def market_breadth() -> dict[str, Any]:
    """涨跌家数统计。"""
    row = db.query_one(
        "SELECT "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up, "
        "SUM(CASE WHEN pct_change < 0 THEN 1 ELSE 0 END) AS down, "
        "SUM(CASE WHEN pct_change = 0 THEN 1 ELSE 0 END) AS flat, "
        "SUM(CASE WHEN pct_change >= 9.8 THEN 1 ELSE 0 END) AS limit_up, "
        "SUM(CASE WHEN pct_change <= -9.8 THEN 1 ELSE 0 END) AS limit_down "
        "FROM market_snapshot WHERE asset_type='stock'"
    )
    d = dict(row) if row else {}
    total = d.get("total") or 0
    up = d.get("up") or 0
    return {
        "total": total,
        "up": up,
        "down": d.get("down") or 0,
        "flat": d.get("flat") or 0,
        "limit_up": d.get("limit_up") or 0,
        "limit_down": d.get("limit_down") or 0,
        "up_ratio": round(up / total * 100, 1) if total else 0.0,
    }
