"""选股筛选器。

两段式设计（兼顾速度与灵活度）：
  第一阶段：在本地全市场快照（SQLite）上做基本面/行情字段过滤 —— 毫秒级
  第二阶段：对候选股拉 K线做技术面过滤 —— 受限额保护，避免打爆数据源
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..symbols import SymbolError, asset_type, board_of
from ..sources import market
from ..sources.base import FetchError, swallow
from . import indicators as ta

log = logging.getLogger("stocklab.screener")

# 可直接在 SQL 上过滤的字段
SNAPSHOT_FIELDS = {
    "price": "最新价",
    "pct_change": "涨跌幅%",
    "amount": "成交额(元)",
    "volume": "成交量(手)",
    "turnover_rate": "换手率%",
    "pe": "市盈率",
    "pb": "市净率",
    "market_cap": "总市值(元)",
    "float_cap": "流通市值(元)",
    "amplitude": "振幅%",
    "change": "涨跌额",
}

OPS = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "!=",
}

# 需要 K线的技术条件
TECH_CONDITIONS = {
    "ma_golden": "均线金叉(MA5上穿MA20)",
    "ma_bull": "均线多头排列(MA5>MA10>MA20)",
    "above_ma20": "站上20日均线",
    "above_ma60": "站上60日均线",
    "macd_golden": "MACD 金叉",
    "macd_bull": "MACD 红柱(DIF>DEA)",
    "kdj_golden": "KDJ 金叉",
    "rsi_oversold": "RSI6 超卖(<30)",
    "rsi_overbought": "RSI6 超买(>70)",
    "vol_surge": "放量(量比>1.8)",
    "new_high_60": "创60日新高",
    "new_low_60": "创60日新低",
    "boll_lower": "触及布林下轨",
    "boll_upper": "触及布林上轨",
    "up_streak": "连续上涨",
}


def _build_sql(conditions: list[dict], kind: str, exclude_st: bool, exclude_new: bool) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    if kind == "a_share":
        where.append("asset_type IN ('stock')")
    elif kind == "etf":
        where.append("asset_type='etf'")
    elif kind == "index":
        where.append("asset_type='index'")
    elif kind == "hk":
        where.append("asset_type='hk'")
    elif kind == "stock":
        where.append("asset_type='stock'")

    if exclude_st:
        where.append("name NOT LIKE '%ST%' AND name NOT LIKE '%退%'")
    if exclude_new:
        # 名称含 C 的通常是次新股（东财用 C 前缀）
        where.append("name NOT LIKE 'C%' AND name NOT LIKE 'N%'")

    for c in conditions:
        f = c.get("field")
        op = c.get("op", "gt")
        val = c.get("value")
        if f not in SNAPSHOT_FIELDS or op not in OPS or val is None:
            continue
        where.append(f"({f} IS NOT NULL AND {f} {OPS[op]} ?)")
        params.append(val)

    return (" AND ".join(where) if where else "1=1"), params


def _passes_tech(bars: list[dict], cond: str, params: dict) -> bool:
    if len(bars) < 25:
        return False
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] or 0 for b in bars]
    c = closes[-1]

    if cond == "ma_golden":
        f = ta.arr_or_nan(ta.sma(closes, 5))
        s = ta.arr_or_nan(ta.sma(closes, 20))
        if len(closes) < 21:
            return False
        import math
        if any(math.isnan(v) for v in (f[-1], s[-1], f[-2], s[-2])):
            return False
        return f[-2] <= s[-2] and f[-1] > s[-1]

    if cond == "ma_bull":
        m5 = ta.arr_or_nan(ta.sma(closes, 5))[-1]
        m10 = ta.arr_or_nan(ta.sma(closes, 10))[-1]
        m20 = ta.arr_or_nan(ta.sma(closes, 20))[-1]
        import math
        if any(math.isnan(v) for v in (m5, m10, m20)):
            return False
        return m5 > m10 > m20

    if cond == "above_ma20":
        m = ta.arr_or_nan(ta.sma(closes, 20))[-1]
        import math
        return not math.isnan(m) and c > m

    if cond == "above_ma60":
        if len(closes) < 60:
            return False
        m = ta.arr_or_nan(ta.sma(closes, 60))[-1]
        import math
        return not math.isnan(m) and c > m

    if cond in ("macd_golden", "macd_bull"):
        m = ta.macd(closes)
        dif = ta.arr_or_nan([v if v is not None else float("nan") for v in m["dif"]])
        dea = ta.arr_or_nan([v if v is not None else float("nan") for v in m["dea"]])
        import math
        if any(math.isnan(v) for v in (dif[-1], dea[-1])):
            return False
        if cond == "macd_bull":
            return dif[-1] > dea[-1]
        if any(math.isnan(v) for v in (dif[-2], dea[-2])):
            return False
        return dif[-2] <= dea[-2] and dif[-1] > dea[-1]

    if cond == "kdj_golden":
        k = ta.kdj(highs, lows, closes)
        kk = ta.arr_or_nan([v if v is not None else float("nan") for v in k["k"]])
        dd = ta.arr_or_nan([v if v is not None else float("nan") for v in k["d"]])
        import math
        if any(math.isnan(v) for v in (kk[-1], dd[-1], kk[-2], dd[-2])):
            return False
        return kk[-2] <= dd[-2] and kk[-1] > dd[-1]

    if cond in ("rsi_oversold", "rsi_overbought"):
        r = ta.rsi(closes, (6,))["rsi6"][-1]
        import math
        if r is None or (isinstance(r, float) and math.isnan(r)):
            return False
        return r < 30 if cond == "rsi_oversold" else r > 70

    if cond == "vol_surge":
        vma = ta.arr_or_nan(ta.sma(vols, 5))[-1]
        import math
        if math.isnan(vma) or vma == 0:
            return False
        return vols[-1] / vma > float(params.get("threshold", 1.8))

    if cond == "new_high_60":
        n = min(60, len(highs))
        return c >= max(highs[-n:]) - 1e-9

    if cond == "new_low_60":
        n = min(60, len(lows))
        return c <= min(lows[-n:]) + 1e-9

    if cond in ("boll_lower", "boll_upper"):
        b = ta.boll(closes)
        up, low = b["upper"][-1], b["lower"][-1]
        if up is None or low is None:
            return False
        return c <= low if cond == "boll_lower" else c >= up

    if cond == "up_streak":
        n = int(params.get("n", 3))
        if len(closes) <= n:
            return False
        return all(closes[-i] > closes[-i - 1] for i in range(1, n + 1))

    return False


def _pe_caveat(conditions: list[dict], results: list[dict]) -> list[str]:
    """负 PE 提示。

    PE 为负 = 公司亏损。所以「PE < 10」这类筛选会**在数学上**把亏损股一起选进来，
    用户以为在找低估值，实际拿到一堆亏损公司（实测踩到：芯原股份 PE=-131.58）。
    用户显式写了 pe>0 时不提示。
    """
    pe_conds = [c for c in conditions if c.get("field") == "pe"]
    if not pe_conds:
        return []
    has_positive = any(
        c.get("op") in ("gt", "gte") and isinstance(c.get("value"), (int, float))
        and c["value"] >= 0
        for c in pe_conds
    )
    if has_positive:
        return []
    negatives = [r for r in results if isinstance(r.get("pe"), (int, float)) and r["pe"] < 0]
    if not negatives:
        return []
    return [
        f"结果中有 {len(negatives)} 只 PE 为负（即亏损公司），例如 "
        + "、".join(f"{r['name']}(PE={r['pe']:.1f})" for r in negatives[:3])
        + "。若只想看盈利公司，请额外添加条件「市盈率 > 0」。"
    ]


def screen(
    conditions: list[dict] | None = None,
    tech: list[str] | None = None,
    tech_params: dict | None = None,
    kind: str = "a_share",
    exclude_st: bool = True,
    exclude_new: bool = True,
    order: str = "amount DESC",
    limit: int = 50,
    tech_scan_limit: int = 150,
) -> dict:
    """执行选股。

    conditions: [{"field":"pe","op":"lt","value":30}, ...]
    tech:       ["ma_golden","vol_surge", ...]

    tech_scan_limit: 技术面过滤需要逐只拉 K线，受数据源频控限制。
                     150 只约需 60~90 秒；K线有 15 分钟进程内缓存，
                     重复执行同一方案会快很多。
    """
    conditions = conditions or []
    tech = tech or []
    tech_params = tech_params or {}

    # 校验输入，把"被忽略的东西"明确报出来。
    # 否则用户把字段名写错（比如 "PE" 写成 "pe_ratio"）会得到全部股票，
    # 看起来像"选股很宽松"，实际是条件根本没生效。
    warnings: list[str] = []
    valid_conditions = []
    for c in conditions:
        f = (c or {}).get("field")
        op = (c or {}).get("op", "gt")
        val = (c or {}).get("value")
        if f not in SNAPSHOT_FIELDS:
            warnings.append(f"未知字段 '{f}'，该条件已忽略（可用字段见 /api/screen/presets）")
            continue
        if op not in OPS:
            warnings.append(f"未知操作符 '{op}'，该条件已忽略")
            continue
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
            warnings.append(f"字段 '{f}' 的取值 {val!r} 不是数字，该条件已忽略")
            continue
        valid_conditions.append(c)
    conditions = valid_conditions

    unknown_tech = [t for t in tech if t not in TECH_CONDITIONS]
    for t in unknown_tech:
        warnings.append(f"未知技术条件 '{t}'，已忽略")
    tech = [t for t in tech if t in TECH_CONDITIONS]

    where, params = _build_sql(conditions, kind, exclude_st, exclude_new)
    # 技术面过滤需要拉 K线，先放大候选池
    pool_limit = limit if not tech else min(max(tech_scan_limit, limit), 800)

    rows = db.rows_to_dicts(
        db.query(
            f"SELECT * FROM market_snapshot WHERE {where} ORDER BY {order} LIMIT ?",
            (*params, pool_limit),
        )
    )

    if not tech:
        for r in rows:
            r["board"] = board_of(r["symbol"])
        warnings.extend(_pe_caveat(conditions, rows[:limit]))
        return {
            "total_candidates": len(rows),
            "returned": len(rows[:limit]),
            "tech_scanned": 0,
            "results": rows[:limit],
            "tech_conditions": [],
            "warnings": warnings,
        }

    passed: list[dict] = []
    scanned = 0
    for r in rows:
        if len(passed) >= limit:
            break
        sym = r["symbol"]
        if r.get("asset_type") in ("index",):
            continue
        scanned += 1
        try:
            bars = market.get_kline(sym, "day", 130)
        except FetchError as exc:
            # 网络/上游问题：这只跳过，继续下一只
            log.debug("选股取K线失败 %s: %s", sym, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            # 编程错误绝不能被吞掉 —— 否则每只股票都静默失败，
            # 用户只会看到「选股结果为空」，完全无从排查。
            swallow(exc, log, f"选股取K线 {sym}", expected=(SymbolError,))
            continue
        if not bars or len(bars) < 25:
            continue
        if all(_passes_tech(bars, t, tech_params.get(t, {})) for t in tech):
            r = dict(r)
            r["board"] = board_of(sym)
            snap = ta.latest_snapshot(bars)
            r["tech_score"] = snap.get("score")
            r["tech_rating"] = snap.get("rating")
            r["signals"] = [s["text"] for s in snap.get("signals", [])]
            passed.append(r)

    warnings.extend(_pe_caveat(conditions, passed))
    return {
        "total_candidates": len(rows),
        "returned": len(passed),
        "tech_scanned": scanned,
        "results": passed,
        "tech_conditions": [TECH_CONDITIONS.get(t, t) for t in tech],
        "warnings": warnings,
    }


def presets() -> list[dict]:
    """内置常用选股方案。"""
    return [
        {
            "key": "value_blue_chip",
            "name": "低估值蓝筹",
            "desc": "PE 5~20、PB<3、市值>500亿、成交活跃",
            "conditions": [
                {"field": "pe", "op": "gt", "value": 0},
                {"field": "pe", "op": "lt", "value": 20},
                {"field": "pb", "op": "lt", "value": 3},
                {"field": "market_cap", "op": "gt", "value": 50_000_000_000},
                {"field": "amount", "op": "gt", "value": 200_000_000},
            ],
            "tech": [],
        },
        {
            "key": "golden_cross",
            "name": "均线金叉",
            "desc": "MA5 上穿 MA20，且站上 60 日线",
            "conditions": [
                {"field": "amount", "op": "gt", "value": 100_000_000},
                {"field": "market_cap", "op": "gt", "value": 5_000_000_000},
            ],
            "tech": ["ma_golden", "above_ma60"],
        },
        {
            "key": "volume_breakout",
            "name": "放量突破",
            "desc": "量比>2 且创 60 日新高",
            "conditions": [
                {"field": "amount", "op": "gt", "value": 100_000_000},
                {"field": "pct_change", "op": "gt", "value": 2},
            ],
            "tech": ["vol_surge", "new_high_60"],
        },
        {
            "key": "oversold_rebound",
            "name": "超跌反弹",
            "desc": "RSI6<30 且触及布林下轨",
            "conditions": [
                {"field": "market_cap", "op": "gt", "value": 5_000_000_000},
                {"field": "amount", "op": "gt", "value": 50_000_000},
            ],
            "tech": ["rsi_oversold", "boll_lower"],
        },
        {
            "key": "strong_momentum",
            "name": "强势多头",
            "desc": "均线多头排列 + MACD 红柱 + 放量",
            "conditions": [
                {"field": "amount", "op": "gt", "value": 200_000_000},
                {"field": "market_cap", "op": "gt", "value": 10_000_000_000},
            ],
            "tech": ["ma_bull", "macd_bull"],
        },
        {
            "key": "low_pe_growth",
            "name": "低估值+高换手",
            "desc": "PE<15、换手率 3%~15%、振幅适中",
            "conditions": [
                {"field": "pe", "op": "gt", "value": 0},
                {"field": "pe", "op": "lt", "value": 15},
                {"field": "turnover_rate", "op": "gt", "value": 3},
                {"field": "turnover_rate", "op": "lt", "value": 15},
                {"field": "amount", "op": "gt", "value": 100_000_000},
            ],
            "tech": [],
        },
    ]


def save_screen(name: str, conditions: list[dict], tech: list[str] | None = None) -> int:
    import json
    return db.execute(
        "INSERT INTO screens(name,conditions) VALUES(?,?)",
        (name, json.dumps({"conditions": conditions, "tech": tech or []}, ensure_ascii=False)),
    )


def list_screens() -> list[dict]:
    import json
    out = []
    for r in db.query("SELECT * FROM screens ORDER BY id DESC"):
        d = dict(r)
        try:
            payload = json.loads(d.get("conditions") or "{}")
            if isinstance(payload, list):
                d["conditions"], d["tech"] = payload, []
            else:
                d["conditions"] = payload.get("conditions", [])
                d["tech"] = payload.get("tech", [])
        except (ValueError, TypeError):
            d["conditions"], d["tech"] = [], []
        out.append(d)
    return out


def delete_screen(sid: int) -> None:
    db.execute("DELETE FROM screens WHERE id=?", (sid,))
