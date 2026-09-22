"""下单参数清单：把系统的结论变成 App 里照着填的数字。

## 为什么需要它

系统算出的止损价、止盈价、股数，最终要落到券商 App 里手动下单。
中间隔着一次"人肉转录"，而转录最容易出错的地方有两个：

  1. **数字抄错**（23.60 抄成 23.06）
  2. **两边不一致** —— 系统里算的是 A，App 里设的是 B，
     等到触发时才发现对不上，而那时已经亏了

所以这里不做任何新计算，只做一件事：**把系统里已经确定的参数
按"下单时需要填的字段"重新排一次**，让人对着填、抄得准。

## 关于直连券商

本系统**不接、也不会接**券商交易接口 —— 那需要用户的交易密码，
而且没有任何公开接口。这里的边界很清楚：
系统只负责把参数整理好，下单动作永远由人在自己的 App 里完成。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .. import db

log = logging.getLogger("stocklab.ordersheet")


def _px(v: Any) -> str:
    """价格统一两位小数 —— 下单清单上"23.6"和"23.60"读起来不一样，
    抄的时候少一位多一位都容易出错，所以固定格式。"""
    f = _f(v)
    return f"{f:.2f}" if f is not None else "—"


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def build(include_watchlist: bool = True) -> dict[str, Any]:
    """生成下单参数清单。"""
    from ..sources import market
    from . import trades as trade_svc
    cash = (trade_svc.get_settings() or {}).get("available_cash")

    positions: list[dict] = []
    rows = db.rows_to_dicts(db.query(
        "SELECT * FROM trades WHERE status='open' ORDER BY entry_date DESC"))
    symbols = [r["symbol"] for r in rows]
    quotes: dict[str, dict] = {}
    if symbols:
        try:
            quotes = market.get_quotes(list(dict.fromkeys(symbols)))
        except Exception as exc:  # noqa: BLE001
            log.debug("下单清单取现价失败: %s", exc)

    for r in rows:
        q = quotes.get(r["symbol"]) or {}
        cur = _f(q.get("price"))
        e = _f(r["entry_price"])
        stop = _f(r["stop_price"])
        tgt = _f(r["target_price"])
        shares = _f(r["shares"]) or 0
        # 「可买股数」和券商 App 里那个「可买__股」是同一个口径：
        # 可用资金 ÷ 委托价，向下取整到整手。让用户能直接比股数，
        # 而不是拿金额去比股数（那样还得自己心算）。
        afford = trade_svc.affordable_shares(e, cash)
        pos: dict[str, Any] = {
            "id": r["id"], "symbol": r["symbol"], "name": r["name"],
            "entry_price": e, "shares": shares,
            "last_price": cur,
            "affordable_shares": afford,
            "enough_cash": None if afford is None else (shares <= afford),
            "pnl_pct": round((cur / e - 1) * 100, 2) if (cur and e) else None,
            "stop": None, "target": None,
        }
        # 止损单：跌破触发 → 卖出
        if stop:
            pos["stop"] = {
                "trigger": stop,
                "direction": "跌破",
                "action": "卖出",
                "qty": shares,
                "distance_pct": round((stop / cur - 1) * 100, 2) if cur else None,
                "note": "触发即离场，不要临场改主意",
            }
        # 止盈单：涨破触发 → 卖出
        if tgt:
            pos["target"] = {
                "trigger": tgt,
                "direction": "涨破",
                "action": "卖出",
                "qty": shares,
                "distance_pct": round((tgt / cur - 1) * 100, 2) if cur else None,
                "note": "可分批止盈",
            }
        positions.append(pos)

    watch: list[dict] = []
    if include_watchlist:
        wrows = db.rows_to_dicts(db.query(
            "SELECT symbol, name FROM watchlist ORDER BY sort_order, id"))
        codes = [w["symbol"] for w in wrows]
        wq: dict[str, dict] = {}
        if codes:
            try:
                wq = market.get_quotes(codes)
            except Exception as exc:  # noqa: BLE001
                log.debug("自选股取价失败: %s", exc)
        for w in wrows:
            q = wq.get(w["symbol"]) or {}
            watch.append({
                "symbol": w["symbol"], "name": w.get("name") or q.get("name"),
                "price": _f(q.get("price")), "pct_change": _f(q.get("pct_change")),
            })

    return {
        "ok": True,
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "positions": positions,
        "watchlist": watch,
        "available_cash": cash,
        "text": render_text(positions, watch),
        "disclaimer": ("本清单只把系统里已确定的参数重新排版，不做新计算。"
                       "系统不接券商接口，下单请在你自己的 App 里手动完成。"),
    }


def render_text(positions: list[dict], watch: list[dict]) -> str:
    """渲染成下单时**照着填**的文本。

    字段名刻意和券商 App 下单界面的输入框一致：
        「股票名称或代码」/「委托价」/「委托量」
    因为对着手机填的时候，最怕的是在一段话里找数字、或者
    看到一个字段名但不知道对应哪个输入框。
    """
    lines: list[str] = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"下单参数清单  {now}")
    lines.append("=" * 46)
    lines.append("字段对应 App 下单界面：股票名称或代码 / 委托价 / 委托量")
    lines.append("")

    if positions:
        lines.append("【买入下单】")
        for p in positions:
            code = str(p["symbol"]).split(".")[0]
            cost = (p["entry_price"] or 0) * (p["shares"] or 0)
            lines.append("")
            lines.append(f"  {p['name']}   {p['symbol']}")
            lines.append("  股票名称或代码    " + code)
            lines.append("  委托价            " + _px(p["entry_price"]) + "   （限价委托）")
            lines.append("  委托量            " + f"{int(p['shares'])} 股")
            lines.append("  所需资金          " + f"{cost:,.0f} 元")
            if p.get("affordable_shares") is not None:
                ok = p.get("enough_cash")
                lines.append("  App「可买」        " + f"{p['affordable_shares']} 股"
                             + ("   ✓ 够" if ok else "   ✗ 不够，按这个数减量"))
            else:
                lines.append("  （在「交易」页填一次可用资金，这里会算出可买股数）")
            lines.append("  " + "-" * 40)
            lines.append("  【卖出下单 · 离场时照这个填】")
            if p.get("stop"):
                st = p["stop"]
                lines.append("  止损单")
                lines.append("    委托价          " + _px(st["trigger"])
                             + f"   （{st['direction']} {_px(st['trigger'])} 时执行）")
                lines.append("    委托量          " + f"{int(st['qty'])} 股")
                if st.get("distance_pct") is not None:
                    lines.append("    距现价          " + f"{st['distance_pct']:+.2f}%")
            if p.get("target"):
                tg = p["target"]
                lines.append("  止盈单")
                lines.append("    委托价          " + _px(tg["trigger"])
                             + f"   （{tg['direction']} {_px(tg['trigger'])} 时执行）")
                lines.append("    委托量          " + f"{int(tg['qty'])} 股")
                if tg.get("distance_pct") is not None:
                    lines.append("    距现价          " + f"{tg['distance_pct']:+.2f}%")
            if p.get("pnl_pct") is not None:
                lines.append("  当前浮动          " + f"{p['pnl_pct']:+.2f}%")
    else:
        lines.append("【买入下单】")
        lines.append("  暂无持仓 — 在「交易」页记录开仓后，这里会自动出现参数")
        lines.append("")

    if watch:
        lines.append("")
        lines.append("=" * 46)
        lines.append(f"【自选股】共 {len(watch)} 只")
        lines.append("")
        for w in watch:
            px = _px(w["price"]) if w["price"] is not None else "—"
            pc = f"{w['pct_change']:+.2f}%" if w["pct_change"] is not None else ""
            lines.append(f"  {str(w['symbol']).split('.')[0]:<8} {str(w.get('name') or ''):<12} {px:>9} {pc:>8}")
        lines.append("")
        lines.append("  委托用代码（6 位，直接粘到「股票名称或代码」框）：")
        lines.append("  " + " ".join(str(w["symbol"]).split(".")[0] for w in watch))
        lines.append("")
        lines.append("  完整代码（需要区分市场时用）：")
        lines.append("  " + " ".join(w["symbol"] for w in watch))

    lines.append("")
    lines.append("=" * 46)
    if not positions and not watch:
        lines.append("清单为空：先加自选股，或在交易页记录开仓。")
        lines.append("")
    lines.append("本系统不接券商交易接口，下单动作请在自己的券商 App 里手动完成。")
    lines.append("若 App 没有条件单功能，就是「提醒响了 → 打开 App → 照上面三个框填」。")
    lines.append("清单只把已确定的参数重新排版，不做新计算；参数随行情变化，请以当时系统为准。")
    return "\n".join(lines)
