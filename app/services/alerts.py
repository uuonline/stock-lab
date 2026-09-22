"""提醒规则引擎。

规则类型：
  price_above  价格上穿阈值
  price_below  价格下穿阈值
  pct_up       涨幅超过阈值
  pct_down     跌幅超过阈值
  vol_surge    放量（量比超过阈值）
  golden_cross 均线金叉
  death_cross  均线死叉
  near_high    接近 N 日新高
  near_low     接近 N 日新低
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import db
from ..symbols import display_name, normalize
from ..sources import market
from . import indicators as ta
from . import notify

log = logging.getLogger("stocklab.alerts")

RULE_TYPES = {
    "price_above": "价格上穿",
    "price_below": "价格下穿",
    "pct_up": "涨幅超过(%)",
    "pct_down": "跌幅超过(%)",
    "vol_surge": "量比超过",
    "golden_cross": "均线金叉(MA5/MA20)",
    "death_cross": "均线死叉(MA5/MA20)",
    "near_high": "接近N日新高(%)",
    "near_low": "接近N日新低(%)",
}


# 每种规则的必填参数 —— 缺了就创建出一条永远无法触发的"死规则"，
# 用户会以为提醒设好了，实际永远不会响。
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "price_above": ("value",),
    "price_below": ("value",),
    "pct_up": ("value",),
    "pct_down": ("value",),
    "vol_surge": (),          # value 可选，默认 2.0
    "golden_cross": (),       # fast/slow 可选，默认 5/20
    "death_cross": (),
    "near_high": (),          # n/within 可选
    "near_low": (),
}


def add_alert(
    symbol: str,
    rule_type: str,
    params: dict[str, Any],
    name: str = "",
    message: str = "",
    cooldown: int = 1800,
    trade_id: int | None = None,
) -> int:
    sym = normalize(symbol)
    if rule_type not in RULE_TYPES:
        raise ValueError(f"未知规则类型: {rule_type}")

    params = dict(params or {})
    missing = [k for k in REQUIRED_PARAMS.get(rule_type, ()) if params.get(k) is None]
    if missing:
        label = RULE_TYPES.get(rule_type, rule_type)
        raise ValueError(
            f"规则「{label}」缺少必填参数: {', '.join(missing)}；"
            f"否则该规则永远不会触发"
        )
    # 数值参数做一次类型校验，避免存入 "abc" 这类值
    for k, v in list(params.items()):
        if v is None or isinstance(v, bool):
            params.pop(k, None)
            continue
        if not isinstance(v, (int, float)):
            raise ValueError(f"参数 {k} 必须是数字，收到 {v!r}")
    if not name:
        try:
            q = market.get_quote(sym)
            name = (q or {}).get("name") or display_name(sym)
        except Exception:  # noqa: BLE001
            name = display_name(sym)
    return db.execute(
        "INSERT INTO alerts(symbol,name,rule_type,params,message,cooldown,trade_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (sym, name, rule_type, json.dumps(params, ensure_ascii=False), message,
         cooldown, trade_id),
    )


def delete_alerts_for_trade(trade_id: int) -> int:
    """删掉某笔交易自动创建的提醒。

    交易结束后这些提醒就是"死规则"：价格早已远离，规则永远不会再触发，
    留着只会让提醒列表越来越长、真规则被淹没。
    """
    rows = db.query("SELECT id FROM alerts WHERE trade_id=?", (trade_id,))
    for r in rows:
        db.execute("DELETE FROM alerts WHERE id=?", (r["id"],))
        db.execute("DELETE FROM alert_events WHERE alert_id=?", (r["id"],))
    return len(rows)


def list_alerts_for_trade(trade_id: int) -> list[dict]:
    return db.rows_to_dicts(db.query(
        "SELECT id, rule_type, params, enabled, fired_count FROM alerts "
        "WHERE trade_id=? ORDER BY id", (trade_id,)))


def list_alerts(only_enabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM alerts"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id DESC"
    out = []
    for r in db.query(sql):
        d = dict(r)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except (ValueError, TypeError):
            d["params"] = {}
        d["rule_label"] = RULE_TYPES.get(d["rule_type"], d["rule_type"])
        out.append(d)
    return out


def toggle_alert(aid: int, enabled: bool) -> None:
    db.execute("UPDATE alerts SET enabled=? WHERE id=?", (1 if enabled else 0, aid))


def delete_alert(aid: int) -> None:
    db.execute("DELETE FROM alerts WHERE id=?", (aid,))


def _fmt_rule(a: dict) -> str:
    p = a.get("params") or {}
    label = RULE_TYPES.get(a["rule_type"], a["rule_type"])
    if a["rule_type"] in ("price_above", "price_below"):
        return f"{label} {p.get('value')}"
    if a["rule_type"] in ("pct_up", "pct_down"):
        return f"{label} {p.get('value')}"
    if a["rule_type"] == "vol_surge":
        return f"{label} {p.get('value', 2.0)}"
    if a["rule_type"] in ("near_high", "near_low"):
        return f"{label.replace('(%)', '')} {p.get('n', 60)}日 / {p.get('within', 2)}%以内"
    return f"{label} MA{p.get('fast', 5)}/MA{p.get('slow', 20)}"


def _evaluate(alert: dict, quote: dict, bars: list[dict] | None) -> str | None:
    """命中返回提示文案，否则 None。"""
    rt = alert["rule_type"]
    p = alert.get("params") or {}
    price = quote.get("price")
    pct = quote.get("pct_change")
    if price is None:
        return None

    if rt == "price_above":
        v = p.get("value")
        return f"现价 {price} 上穿 {v}" if v is not None and price >= v else None

    if rt == "price_below":
        v = p.get("value")
        return f"现价 {price} 下穿 {v}" if v is not None and price <= v else None

    if rt == "pct_up":
        v = p.get("value")
        return f"涨幅 {pct}% 超过 {v}%" if v is not None and pct is not None and pct >= v else None

    if rt == "pct_down":
        v = p.get("value")
        return f"跌幅 {pct}% 超过 {v}%" if v is not None and pct is not None and pct <= -abs(v) else None

    if rt == "vol_surge":
        v = p.get("value", 2.0)
        vr = quote.get("vol_ratio")
        return f"量比 {vr} 超过 {v}" if vr is not None and vr >= v else None

    if rt in ("golden_cross", "death_cross", "near_high", "near_low"):
        if not bars or len(bars) < 25:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]

        if rt in ("golden_cross", "death_cross"):
            fast = int(p.get("fast", 5))
            slow = int(p.get("slow", 20))
            f = ta.arr_or_nan(ta.sma(closes, fast))
            s = ta.arr_or_nan(ta.sma(closes, slow))
            import math
            if any(math.isnan(v) for v in (f[-1], s[-1], f[-2], s[-2])):
                return None
            if rt == "golden_cross" and f[-2] <= s[-2] and f[-1] > s[-1]:
                return f"MA{fast} 上穿 MA{slow}（金叉）"
            if rt == "death_cross" and f[-2] >= s[-2] and f[-1] < s[-1]:
                return f"MA{fast} 下穿 MA{slow}（死叉）"
            return None

        n = int(p.get("n", 60))
        within = float(p.get("within", 2.0)) / 100.0
        n = min(n, len(closes))
        if rt == "near_high":
            hi = max(highs[-n:])
            if hi > 0 and price >= hi * (1 - within):
                return f"现价 {price} 接近 {n} 日新高 {hi}"
        else:
            lo = min(lows[-n:])
            if lo > 0 and price <= lo * (1 + within):
                return f"现价 {price} 接近 {n} 日新低 {lo}"
        return None

    return None


def check_all_quotes() -> list[dict]:
    """巡检全部启用规则，返回本次触发的提醒。"""
    alerts = list_alerts(only_enabled=True)
    if not alerts:
        return []

    now = time.time()
    due = [a for a in alerts if now - (a.get("last_fired") or 0) >= (a.get("cooldown") or 1800)]
    if not due:
        return []

    symbols = sorted({a["symbol"] for a in due})
    quotes = market.get_quotes(symbols)

    need_bars = {a["symbol"] for a in due if a["rule_type"] in ("golden_cross", "death_cross", "near_high", "near_low")}
    bars_cache: dict[str, list[dict]] = {}
    for sym in need_bars:
        try:
            bars_cache[sym] = market.get_kline(sym, "day", 130)
        except Exception:  # noqa: BLE001
            bars_cache[sym] = []

    fired: list[dict] = []
    for a in due:
        q = quotes.get(a["symbol"])
        if not q:
            continue
        try:
            hit = _evaluate(a, q, bars_cache.get(a["symbol"]))
        except Exception as exc:  # noqa: BLE001
            log.debug("规则评估失败 #%s: %s", a["id"], exc)
            continue
        if not hit:
            continue

        text = a.get("message") or f"{a['name']}（{a['symbol']}）触发提醒"
        content = (
            f"**{a['name']}**（{a['symbol']}）\n\n"
            f"- 触发条件：{_fmt_rule(a)}\n"
            f"- 实际情况：{hit}\n"
            f"- 最新价：{q.get('price')}（{q.get('pct_change')}%）\n"
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"{text}"
        )
        db.execute(
            "UPDATE alerts SET last_fired=?, fired_count=fired_count+1 WHERE id=?",
            (now, a["id"]),
        )
        db.execute(
            "INSERT INTO alert_events(alert_id,symbol,name,rule_type,message,price) VALUES(?,?,?,?,?,?)",
            (a["id"], a["symbol"], a["name"], a["rule_type"], hit, q.get("price")),
        )
        try:
            notify.send(content, title=f"StockLab 提醒 · {a['name']}")
        except Exception as exc:  # noqa: BLE001
            log.warning("提醒推送失败: %s", exc)

        fired.append({
            "alert_id": a["id"], "symbol": a["symbol"], "name": a["name"],
            "rule": _fmt_rule(a), "hit": hit, "price": q.get("price"),
            "pct_change": q.get("pct_change"),
        })

    return fired


def recent_events(limit: int = 50) -> list[dict]:
    return db.rows_to_dicts(
        db.query("SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (limit,))
    )


def clear_events() -> None:
    db.execute("DELETE FROM alert_events")


def test_notify() -> dict[str, bool]:
    """发送一条测试消息到所有已配置渠道。"""
    content = (
        f"**StockLab 推送测试**\n\n"
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 说明：如果你看到这条消息，说明提醒渠道配置成功。"
    )
    return notify.send(content, title="StockLab 测试消息")
