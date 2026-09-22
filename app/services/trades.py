"""交易日志与仓位计算。

## 为什么需要这个

系统前面做的都是「分析」，而分析不等于交易。从分析到交易之间有两件事
系统一直没管，恰好是决定成败的两件：

  1. **仓位**。「这股票能买吗」和「买多少」是两个问题。
     单笔风险控制（比如总资金的 2%）必须**先定**，再反推股数 ——
     顺序反了（先决定买多少再看能亏多少）几乎必然失控。
  2. **复盘**。只看"赚了还是亏了"没有意义，因为那可能只是运气。
     真正能改进的是**判断质量**：哪一类入场理由对你有效、哪一类总是亏。
     所以开仓时必须把当时的分析状态快照存下来（箱体位置、状态判定、
     历史类比），事后才能按"当时是什么情形"分组统计。

## 快照只存分析结论，不重新算新闻

开仓时快照要快（<1 秒），所以只取：箱体（本地 K线算，快）、
异动检测（本地）、历史类比（读本地库）。不取消息面和 F10 ——
那两项慢且与"我的判断依据"关系不大。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from typing import Any

from .. import db

log = logging.getLogger("stocklab.trades")

KV_CASH = "trades:available_cash"


def get_settings() -> dict[str, Any]:
    """账户层面的设置。目前只有可用资金一项。

    为什么需要它：仓位计算器是按**风险**算股数的，它不知道账户里有多少钱。
    算出来 600 股、结果 App 里「可买」只有 400 股 —— 这个断层必须补上，
    否则清单给了参数、下单时才发现买不起。
    """
    v = db.kv_get(KV_CASH)
    try:
        cash = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        cash = None
    return {"available_cash": cash}


def set_settings(available_cash: float | None) -> dict[str, Any]:
    if available_cash is not None:
        c = _f(available_cash)
        if c is None or c < 0:
            return {"ok": False, "reason": "可用资金必须是非负数"}
        db.kv_set(KV_CASH, c)
    return {"ok": True, **get_settings()}


def affordable_shares(price: float | None, cash: float | None) -> int | None:
    """按委托价算「可买多少股」——和券商 App 里的「可买__股」同一个口径。

    A股买入必须是 100 股的整数倍（卖出可以零股，买入不行），所以向下取整到整手。
    """
    p, c = _f(price), _f(cash)
    if not p or p <= 0 or c is None or c <= 0:
        return None
    return int(c // (p * 100)) * 100


# 单笔风险的常规上限（占总资金比例）。超过就提示 —— 不是禁止，
# 但要让人意识到自己在做一件偏离常规的事。
RISK_WARN_PCT = 3.0
# 单只标的的仓位上限
POSITION_WARN_PCT = 30.0


# 编程错误不能和"数据取不到"混为一谈。
# 实测踩过一次：from .anomaly import support_resistance 写错了模块名，
# 抛出的 ImportError 被 except 静默吞掉，目标位悄悄退化成兜底值 ——
# 界面看起来正常，结论却已经不对了。这类错误必须吵出来。
_PROGRAMMING_ERRORS = (ImportError, AttributeError, TypeError, NameError)


def _swallow(exc: Exception, where: str) -> None:
    if isinstance(exc, _PROGRAMMING_ERRORS):
        log.error("编程错误（不是数据问题）%s: %s", where, exc, exc_info=True)
    else:
        log.debug("%s: %s", where, exc)


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ============================================================
# 仓位计算
# ============================================================

def calc_position(capital: float, risk_pct: float, entry: float,
                  stop: float, target: float | None = None,
                  max_position_pct: float = POSITION_WARN_PCT,
                  available_cash: float | None = None,
                  current_price: float | None = None) -> dict[str, Any]:
    """由「能亏多少」反推「该买多少」。

    公式：股数 = 总资金 × 单笔风险% ÷ |入场价 − 止损价|

    这是整个过程里最重要的一步。绝大多数人先想"我有 10 万，买 5 万吧"，
    然后才发现止损位离入场太远、一次就亏掉 8% —— 顺序反了。
    """
    cap, rp = _f(capital), _f(risk_pct)
    e, s = _f(entry), _f(stop)
    if not cap or cap <= 0:
        return {"ok": False, "reason": "总资金必须是正数"}
    if rp is None or rp <= 0 or rp > 100:
        return {"ok": False, "reason": "单笔风险比例应在 0~100 之间"}
    if not e or e <= 0:
        return {"ok": False, "reason": "入场价必须是正数"}
    if not s or s <= 0:
        return {"ok": False, "reason": "止损价必须是正数"}
    if s >= e:
        return {"ok": False, "reason": "止损价必须低于入场价（本系统只做多）"}

    risk_amount = cap * rp / 100.0
    per_share_risk = e - s
    shares = math.floor(risk_amount / per_share_risk)
    # A股按手（100 股）交易；不足一手时按一手算但给出提示
    lots = shares // 100
    shares_rounded = lots * 100
    warn = []
    if shares_rounded <= 0:
        shares_rounded = 0
        warn.append("按这个止损距离，风险预算买不起一手（100 股），"
                    "要么放宽止损、要么增加风险预算、要么放弃这笔")
    # 账户现金约束：按风险算出来的股数可能买不起。
    # 这时**不是直接砍到可买数就算了** —— 砍了之后实际风险会变小，
    # 但止损距离没变，仓位却不再是"风险预算对应的仓位"。
    # 所以要把砍完之后的实际风险也告诉用户。
    afford = affordable_shares(e, available_cash)
    if afford is not None and shares_rounded > afford:
        warn.append(
            f"这只股票按风险预算需要 {shares_rounded} 股（{shares_rounded * e:.0f} 元），"
            f"但账户可用资金 {available_cash:.0f} 元只够 {afford} 股 —— "
            f"已按资金给出可下单股数；实际风险会低于你设定的 {rp:.2f}% 预算"
        )
    cost = shares_rounded * e
    if cost > cap:
        warn.append(f"所需资金 {cost:.0f} 超过总资金 {cap:.0f}，不可行")
    if cap and cost / cap * 100 > max_position_pct:
        warn.append(f"该仓位占总资金 {cost / cap * 100:.1f}%，"
                    f"超过建议上限 {max_position_pct:.0f}%")

    # ---- 直接给出"这笔最多能下多少股" ----
    #
    # 之前只给按风险算出的股数，然后警告"买不起" —— 等于把减法留给用户。
    # 正确的做法是把两个约束取小值，直接给可执行的数字，并说明是哪个约束卡住的。
    cur = _f(current_price)
    afford_cur = affordable_shares(cur, available_cash) if cur else None
    binding = "风险预算"
    final = shares_rounded
    if afford is not None and afford < final:
        final, binding = afford // 100 * 100, "账户资金"
    if final < 0:
        final = 0

    out: dict[str, Any] = {
        "ok": True,
        "capital": cap,
        "risk_pct": rp,
        "risk_amount": round(risk_amount, 2),
        "entry": e, "stop": s,
        "stop_distance_pct": round((e - s) / e * 100, 2),
        "shares": shares_rounded,                 # 按风险预算算出的
        "final_shares": final,                    # 实际可下单（两个约束取小）
        "binding": binding,                       # 哪个约束卡住了
        "lots": final // 100,
        "cost": round(final * e, 2),
        "position_pct": round(final * e / cap * 100, 2) if cap else None,
        "max_loss": round(final * per_share_risk, 2),
        "actual_risk_pct": round(final * per_share_risk / cap * 100, 2) if cap else None,
        "affordable_shares": afford,              # 按入场价
        "affordable_at_current": afford_cur,      # 按现价
        "current_price": cur,
        "available_cash": _f(available_cash),
        "warnings": warn,
    }
    t = _f(target)
    if t and t > e:
        reward = final * (t - e)
        out.update({
            "target": t,
            "reward": round(reward, 2),
            "rr": round((t - e) / per_share_risk, 2),
            "target_gain_pct": round((t - e) / e * 100, 2),
        })
        if out["rr"] < 1.5:
            warn.append(f"盈亏比只有 {out['rr']}，低于 1.5 —— "
                        f"这种赔率下需要很高的胜率才划算")
    return out


# ============================================================
# 开仓时快照
# ============================================================

def snapshot(symbol: str) -> dict[str, Any]:
    """抓一份"我为什么买"的当时状态。只取本地可算的部分，保证快。"""
    snap: dict[str, Any] = {"at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    bars: list[dict] = []
    try:
        from . import flow as flow_svc
        bars = flow_svc.long_history(symbol, 1300)
    except Exception as exc:  # noqa: BLE001
        snap["bars_error"] = str(exc)
    if not bars:
        return snap

    try:
        from . import box as box_svc
        bx = box_svc.adaptive(bars, None)
        a = bx.get("analysis") or {}
        snap["box"] = {
            "window": bx.get("recommended_window"),
            "top": a.get("top"), "bottom": a.get("bottom"),
            "position_pct": a.get("position_pct"), "shape": a.get("shape"),
            "confidence": a.get("confidence"),
            "trustworthy": bx.get("trustworthy"),
        } if a else None
    except Exception as exc:  # noqa: BLE001
        snap["box_error"] = str(exc)

    try:
        from . import anomaly as anom_svc
        det = anom_svc.detect(bars[-300:], {})
        snap["anomaly"] = {k: det.get(k) for k in
                           ("level_name", "z_score", "vol_ratio", "daily_vol_pct")}
    except Exception as exc:  # noqa: BLE001
        snap["anomaly_error"] = str(exc)

    try:
        from . import analogs as ag_svc
        ag = ag_svc.analyze(symbol, bars)
        if ag.get("ok"):
            snap["analogs"] = {
                "events": ag.get("events"), "tier": ag.get("tier"),
                "h5": (ag["horizons"]["5"]["signal"] if ag.get("horizons") else None),
                "h20": (ag["horizons"]["20"]["signal"] if ag.get("horizons") else None),
                "worst": (ag.get("tail") or {}).get("worst"),
            }
    except Exception as exc:  # noqa: BLE001
        snap["analogs_error"] = str(exc)

    closes = [_f(b.get("close")) for b in bars]
    snap["price"] = closes[-1] if closes else None
    if len(closes) >= 60 and closes[-1]:
        ma20 = sum(c for c in closes[-20:] if c) / 20
        snap["ma20"] = round(ma20, 3)
    return snap


# ============================================================
# 增删改查
# ============================================================

def open_trade(symbol: str, entry_price: float, shares: float,
               stop_price: float | None = None, target_price: float | None = None,
               reason: str = "", note: str = "", entry_date: str | None = None,
               name: str = "", plan: dict | None = None,
               take_snapshot: bool = True,
               auto_alerts: bool = True) -> dict[str, Any]:
    from ..sources import market
    sym = market.normalize(symbol)
    if not name:
        # 优先用行情接口给的名字。display_name 走的是本地 instruments 表，
        # 标的没入库时会**回退成代码本身**，界面上就会出现"002241.SZ 002241.SZ"。
        try:
            q = market.get_quote(sym)
            name = (q or {}).get("name") or ""
        except Exception:  # noqa: BLE001
            name = ""
        if not name:
            try:
                name = market.display_name(sym)
            except Exception:  # noqa: BLE001
                name = sym
    e = _f(entry_price)
    if not e or e <= 0:
        return {"ok": False, "reason": "入场价必须是正数"}
    s = _f(stop_price)
    if s is not None and s >= e:
        return {"ok": False, "reason": "止损价必须低于入场价"}
    t = _f(target_price)
    if t is not None and t <= e:
        return {"ok": False, "reason": "目标价必须高于入场价"}

    snap = snapshot(sym) if take_snapshot else {}
    tid = db.execute(
        "INSERT INTO trades(symbol,name,status,entry_date,entry_price,shares,"
        "stop_price,target_price,reason,note,snapshot,plan) "
        "VALUES(?,?,'open',?,?,?,?,?,?,?,?,?)",
        (sym, name, entry_date or dt.date.today().isoformat(), e, _f(shares) or 0,
         s, t, reason or "", note or "",
         json.dumps(snap, ensure_ascii=False),
         json.dumps(plan or {}, ensure_ascii=False)),
    )

    # 把止损价/目标价变成真正的提醒。
    #
    # 在此之前交易计划和提醒是脱节的：你记录了「止损 23.60」，
    # 但价格碰到 23.60 时系统不会告诉你 —— 计划只写在了纸上。
    alerts: list[dict] = []
    if auto_alerts:
        alerts = _create_plan_alerts(tid, sym, name, s, t)
    return {"ok": True, "id": tid, "snapshot": snap, "alerts": alerts}


def _create_plan_alerts(trade_id: int, sym: str, name: str,
                        stop: float | None, target: float | None) -> list[dict]:
    """按交易计划建提醒。失败不影响开仓 —— 记录交易比提醒重要。"""
    from . import alerts as alert_svc
    made: list[dict] = []
    if stop:
        try:
            aid = alert_svc.add_alert(
                sym, "price_below", {"value": stop}, name=name,
                message=f"【止损位】跌破 {stop} —— 按计划这里该离场，不要临场改主意",
                cooldown=300,          # 止损要灵敏，冷却短一些
                trade_id=trade_id,
            )
            made.append({"id": aid, "kind": "stop", "price": stop})
        except Exception as exc:  # noqa: BLE001
            log.warning("创建止损提醒失败 %s: %s", sym, exc)
    if target:
        try:
            aid = alert_svc.add_alert(
                sym, "price_above", {"value": target}, name=name,
                message=f"【目标位】触及 {target} —— 按计划可考虑分批止盈",
                cooldown=1800,
                trade_id=trade_id,
            )
            made.append({"id": aid, "kind": "target", "price": target})
        except Exception as exc:  # noqa: BLE001
            log.warning("创建目标提醒失败 %s: %s", sym, exc)
    return made


def close_trade(trade_id: int, exit_price: float,
                exit_date: str | None = None, note: str = "") -> dict[str, Any]:
    row = db.query_one("SELECT * FROM trades WHERE id=?", (trade_id,))
    if not row:
        return {"ok": False, "reason": "找不到该交易"}
    if row["status"] == "closed":
        return {"ok": False, "reason": "该交易已平仓"}
    x = _f(exit_price)
    if not x or x <= 0:
        return {"ok": False, "reason": "平仓价必须是正数"}
    ed = exit_date or dt.date.today().isoformat()
    db.execute(
        "UPDATE trades SET status='closed', exit_price=?, exit_date=?, "
        "note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
        (x, ed, note, note, trade_id),
    )
    # 平仓后按计划建的提醒就是死规则了，一并清掉
    removed = 0
    try:
        from . import alerts as alert_svc
        removed = alert_svc.delete_alerts_for_trade(trade_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("清理关联提醒失败 trade=%s: %s", trade_id, exc)
    return {"ok": True, "id": trade_id, "alerts_removed": removed}


def delete_trade(trade_id: int) -> dict[str, Any]:
    """删除一条交易记录。

    ⚠️ 不能用 db.execute 的返回值判断"有没有删到" ——
    它返回的是 `lastrowid or rowcount`，而 lastrowid 可能是**上一条 INSERT
    留下的值**，于是删除一条不存在的记录也会返回真值，接口就误报成功。
    所以先查存在性。
    """
    row = db.query_one("SELECT id FROM trades WHERE id=?", (trade_id,))
    if not row:
        return {"ok": False, "reason": "找不到该交易", "deleted": 0}
    removed = 0
    try:
        from . import alerts as alert_svc
        removed = alert_svc.delete_alerts_for_trade(trade_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("清理关联提醒失败 trade=%s: %s", trade_id, exc)
    db.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    return {"ok": True, "deleted": 1, "alerts_removed": removed}


def _hold_days(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return (dt.date.fromisoformat(str(b)[:10]) - dt.date.fromisoformat(str(a)[:10])).days
    except (ValueError, TypeError):
        return None


def _enrich(row: dict, quote: dict | None = None) -> dict:
    """补上收益、R 倍数、持有天数，以及用当前价算的浮动盈亏。"""
    e = _f(row.get("entry_price"))
    s = _f(row.get("stop_price"))
    x = _f(row.get("exit_price"))
    sh = _f(row.get("shares")) or 0
    out = dict(row)
    try:
        out["snapshot"] = json.loads(row.get("snapshot") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["snapshot"] = {}
    try:
        out["plan"] = json.loads(row.get("plan") or "{}")
    except (json.JSONDecodeError, TypeError):
        out["plan"] = {}

    out["risk_per_share"] = round(e - s, 4) if (e and s) else None
    if e and x:
        out["pnl"] = round((x - e) * sh, 2)
        out["pnl_pct"] = round((x / e - 1) * 100, 2)
        out["hold_days"] = _hold_days(row.get("entry_date"), row.get("exit_date"))
        if e and s and e > s:
            out["r_multiple"] = round((x - e) / (e - s), 2)
    elif e and quote:
        cur = _f(quote.get("price"))
        if cur:
            out["last_price"] = cur
            out["pnl"] = round((cur - e) * sh, 2)
            out["pnl_pct"] = round((cur / e - 1) * 100, 2)
            out["hold_days"] = _hold_days(row.get("entry_date"), dt.date.today().isoformat())
            if s and e > s:
                out["r_multiple"] = round((cur - e) / (e - s), 2)
    return out


def list_trades(status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM trades"
    params: list[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY status='open' DESC, COALESCE(exit_date, entry_date) DESC LIMIT ?"
    params.append(limit)
    rows = db.rows_to_dicts(db.query(sql, tuple(params)))

    # 持仓的浮动盈亏要取现价
    quotes: dict[str, dict] = {}
    opens = [r["symbol"] for r in rows if r["status"] == "open"]
    if opens:
        try:
            from ..sources import market
            quotes = market.get_quotes(list(dict.fromkeys(opens)))
        except Exception as exc:  # noqa: BLE001
            log.debug("持仓现价获取失败: %s", exc)
    out = []
    for r in rows:
        item = _enrich(r, quotes.get(r["symbol"]))
        try:
            from . import alerts as alert_svc
            item["alerts"] = alert_svc.list_alerts_for_trade(r["id"])
        except Exception:  # noqa: BLE001
            item["alerts"] = []
        out.append(item)
    return out


def stats() -> dict[str, Any]:
    """按"当时的判断依据"分组统计 —— 这才是复盘要看的东西。

    只看总胜率意义不大：真正的问题是「哪一类入场理由对我有效」。
    所以除了总体，还要按 reason 标签分组。
    """
    rows = [_enrich(r) for r in db.rows_to_dicts(db.query(
        "SELECT * FROM trades WHERE status='closed' ORDER BY exit_date"))]
    opens = list_trades("open")
    if not rows:
        return {"ok": True, "closed": 0, "open": len(opens), "groups": [],
                "note": "还没有已平仓的交易"}

    def agg(items: list[dict]) -> dict[str, Any]:
        pnls = [x["pnl"] for x in items if x.get("pnl") is not None]
        rs = [x["r_multiple"] for x in items if x.get("r_multiple") is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gains = sum(wins)
        loss_sum = -sum(losses)
        return {
            "count": len(items),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else None,
            # 盈亏比 = 平均盈利 / 平均亏损。期望值要为正，需要
            # 胜率 × 盈亏比 > 1 —— 这两者必须一起看，单看一个会误判。
            "profit_factor": (round(gains / loss_sum, 2) if loss_sum > 0 else None),
            "total_pnl": round(sum(pnls), 2) if pnls else None,
            "best": round(max(pnls), 2) if pnls else None,
            "worst": round(min(pnls), 2) if pnls else None,
        }

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("reason") or "未标注理由").strip() or "未标注理由", []).append(r)

    by_group = [{"reason": k, **agg(v)} for k, v in
                sorted(groups.items(), key=lambda kv: -len(kv[1]))]

    # 持有天数分布
    holds = [x["hold_days"] for x in rows if x.get("hold_days") is not None]
    overall = agg(rows)
    overall["avg_hold_days"] = round(sum(holds) / len(holds), 1) if holds else None

    return {
        "ok": True,
        "closed": len(rows), "open": len(opens),
        "overall": overall,
        "groups": by_group,
        "note": ("期望值为正需要「胜率 × 盈亏比 > 1」。"
                 "只看胜率容易被误导：胜率 30% 但盈亏比 3 倍是赚钱的，"
                 "胜率 70% 但盈亏比 0.3 倍是亏钱的。"),
    }


def capacity(symbol: str, cash: float | None = None,
             price: float | None = None) -> dict[str, Any]:
    """「按我的钱和现在的价格，能买多少股」。

    这是下单前的一个快速容量检查：不涉及止损和目标，只回答一个问题 ——
    这个价位上，我的账户能吃下多少。

    注意和仓位计算的分工：**这里算的是"买得起多少"，不是"该买多少"**。
    该买多少由风险预算决定（calc_position），两者取小才是可执行的数量。
    """
    from ..sources import market
    sym = market.normalize(symbol)
    cash_v = _f(cash)
    if cash_v is None:
        cash_v = (get_settings() or {}).get("available_cash")
    px = _f(price)
    src = "手动指定"
    if px is None:
        try:
            q = market.get_quote(sym) or {}
            px = _f(q.get("price"))
            src = "实时行情"
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"取现价失败: {exc}"}
    if not px:
        return {"ok": False, "reason": "拿不到价格"}
    if not cash_v:
        return {"ok": False, "reason": "未设置可用资金（在交易页填一次即可）",
                "price": px}

    shares = affordable_shares(px, cash_v)
    return {
        "ok": True,
        "symbol": sym,
        "price": px,
        "price_source": src,
        "available_cash": cash_v,
        "affordable_shares": shares,
        "lots": (shares or 0) // 100,
        "cost": round((shares or 0) * px, 2),
        "leftover": round(cash_v - (shares or 0) * px, 2),
        "note": ("A股买入需 100 股整数倍，所以有余额剩下是正常的"
                 if shares else "资金不足一手（100 股）"),
    }


def plan(symbol: str, capital: float | None = None, risk_pct: float | None = None,
         stop_override: float | None = None,
         target_override: float | None = None) -> dict[str, Any]:
    """一键仓位方案：现价 → 止损位 → 股数 → 仓位。

    之前的流程要求用户自己填入场价和止损价，但"止损该设在哪"恰好是
    最需要专业判断、新手最容易拍脑袋的一步。而这个系统里有现成的技术位：
    ATR、支撑位、箱体下沿 —— 都可以直接算出来。

    所以这里给出**多个止损候选**并说明各自的依据，再按风险预算算股数，
    最后和账户资金取小，直接给出可下单的数量。
    """
    from ..sources import market
    from . import indicators as ta
    from . import box as box_svc

    sym = market.normalize(symbol)
    st = get_settings() or {}
    cap = _f(capital) or _f(st.get("capital")) or 100000.0
    rp = _f(risk_pct) or 2.0

    try:
        q = market.get_quote(sym) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"行情获取失败: {exc}"}
    price = _f(q.get("price"))
    if not price:
        return {"ok": False, "reason": "拿不到现价"}

    bars: list[dict] = []
    try:
        from .flow import long_history
        bars = long_history(sym, 700)[-300:]
    except Exception as exc:  # noqa: BLE001
        _swallow(exc, f"plan: K线 {sym}")

    # ---- 止损候选：每个都说明依据，不是随便给一个数 ----
    cands: list[dict] = []
    snap = ta.latest_snapshot(bars) if bars else {}
    atr = _f(((snap.get("values") or {}).get("atr14")))
    if atr and atr > 0:
        for mult in (2.0,):
            stp = price - mult * atr
            if stp > 0:
                cands.append({
                    "kind": "ATR 止损", "stop": round(stp, 2),
                    "basis": f"现价 − {mult:g}×ATR14（{atr:.2f}）",
                    "note": "按波动幅度设，适合没有明显支撑位时",
                })
    try:
        from .flow import support_resistance
        sr = support_resistance(bars, price, lookback=120) or {}
        for sup in (sr.get("supports") or [])[:2]:
            if sup["price"] < price:
                cands.append({
                    "kind": "支撑位下方", "stop": round(sup["price"] * 0.995, 2),
                    "basis": f"最近支撑位 {sup['price']} 下方 0.5%",
                    "note": f"该价位历史被触碰 {sup['touches']} 次，跌破才算失效",
                })
    except Exception as exc:  # noqa: BLE001
        _swallow(exc, f"plan: 支撑位 {sym}")
    try:
        bx = box_svc.adaptive(bars, price) if bars else {}
        b = (bx.get("analysis") or {})
        if b.get("bottom") and b["bottom"] < price and bx.get("trustworthy"):
            cands.append({
                "kind": "箱体下沿", "stop": round(b["bottom"] * 0.99, 2),
                "basis": f"箱体下沿 {b['bottom']} 下方 1%",
                "note": f"{bx.get('recommended_window')} 日箱体，跌破意味着区间逻辑失效",
            })
    except Exception as exc:  # noqa: BLE001
        _swallow(exc, f"plan: 箱体 {sym}")

    # 兜底：固定百分比（只在上面都算不出来时用）
    if not cands:
        cands.append({
            "kind": "固定百分比", "stop": round(price * 0.92, 2),
            "basis": "现价下方 8%", "note": "没有技术位可用时的兜底",
        })

    # ---- 目标位 ----
    target = _f(target_override)
    target_basis = "手动指定"
    if not target:
        try:
            res = (support_resistance(bars, price, lookback=120) or {}).get("resistances") or []
            if res and res[0]["price"] > price:
                target = res[0]["price"]
                target_basis = f"最近压力位 {target}（被触碰 {res[0]['touches']} 次）"
        except Exception as exc:  # noqa: BLE001
            _swallow(exc, f"plan: 目标压力位 {sym}")
    if not target:
        try:
            b = (box_svc.adaptive(bars, price) or {}).get("analysis") or {}
            if b.get("top") and b["top"] > price:
                target = b["top"]
                target_basis = f"箱体上沿 {target}"
        except Exception as exc:  # noqa: BLE001
            _swallow(exc, f"plan: 目标箱体 {sym}")
    if not target and atr:
        target = round(price + 3 * atr, 2)
        target_basis = "现价 + 3×ATR14（兜底）"

    # ---- 每个候选算一遍仓位 ----
    cash = st.get("available_cash")
    for c in cands:
        s_ = c["stop"]
        if s_ >= price:
            c["valid"] = False
            c["reason"] = "止损高于现价，不可用"
            continue
        calc = calc_position(cap, rp, price, s_, target, available_cash=cash,
                             current_price=price)
        c.update({
            "valid": calc["ok"],
            "stop_distance_pct": calc.get("stop_distance_pct"),
            "risk_shares": calc.get("shares"),
            "final_shares": calc.get("final_shares"),
            "binding": calc.get("binding"),
            "cost": calc.get("cost"),
            "actual_risk_pct": calc.get("actual_risk_pct"),
            "rr": calc.get("rr"),
            "warnings": calc.get("warnings") or [],
        })

    valid = [c for c in cands if c.get("valid")]
    # 推荐哪一个：距离适中（2%~12%）优先，太近容易被日常波动打掉，
    # 太远则仓位被压得过小、意义不大
    def score(c: dict) -> tuple:
        d = c.get("stop_distance_pct") or 999
        in_band = 2.0 <= d <= 12.0
        return (not in_band, abs(d - 6.0))
    best = min(valid, key=score) if valid else None

    return {
        "ok": True,
        "symbol": sym,
        "name": q.get("name") or market.display_name(sym),
        "price": price,
        "pct_change": _f(q.get("pct_change")),
        "capital": cap,
        "risk_pct": rp,
        "risk_amount": round(cap * rp / 100, 2),
        "available_cash": cash,
        "atr14": atr,
        "target": target,
        "target_basis": target_basis,
        "candidates": cands,
        "recommended": best,
        "note": ("止损位由技术位算出，不是拍脑袋；但**哪个止损适合你，"
                 "取决于你能承受多大回撤** —— 距离越远仓位越小、越不容易被日常波动打掉；"
                 "距离越近仓位越大、但更容易被洗出去。这里只给依据和后果。"),
    }
