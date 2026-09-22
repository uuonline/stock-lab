"""群晖 Chat 命令接口：在 Chat 里直接查系统。

## 边界

这不是"和 AI 聊天"。本系统不是常驻对话服务 —— 它在 Chat 里做的是
**查询**：你发一条命令，它把系统里算好的结果回给你。

好处是出门在外不用开网页：手机上在已配好的频道里发一句话就能查。

## 安全

频道里任何人发消息都会触发传出 Webhook。所以：
  1. 必须在群晖端**校验 token**（本模块用 SL_CHAT_TOKEN 做比对）
  2. 建议把这个 Webhook 挂在**私密频道**上（只有自己能发）
  3. 命令**只读** —— 不做任何写操作（不能下单、不能改设置、不能删记录）。
     万一 token 泄露，最坏情况是别人能看到你的自选股行情，而不是动你的数据。

## 回复格式

群晖 Chat 对 markdown 支持有限，所以用**纯文本 + 换行**，
不依赖 ** 加粗之类。并且控制长度 —— 手机上消息太长不好读。
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("stocklab.chatbot")

# 回复长度上限：手机上好读的极限大概就这么多
MAX_LEN = 900

HELP = """StockLab 命令（只读查询）

查 <代码|名称>    行情 + 箱体位置 + 状态
异动 <代码>       今天有没有异动、什么原因
仓位 <代码>       现价、推荐止损、能买多少股
持仓              当前持仓与止损
自选              自选股行情
帮助              显示这条

例：仓位 002241
也可以直接发代码，如 002241"""


def _cut(text: str) -> str:
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 20] + "\n…（已截断）"


def _num(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _sign(v: Any, digits: int = 2, suffix: str = "%") -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{'+' if f >= 0 else ''}{f:.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _resolve(word: str) -> str | None:
    """把用户输入解析成标准代码：支持 002241 / 002241.SZ / 歌尔股份。"""
    from ..sources import market
    from ..symbols import SymbolError

    w = (word or "").strip()
    if not w:
        return None
    try:
        return market.normalize(w)
    except SymbolError:
        pass
    except Exception:  # noqa: BLE001
        pass
    # 按名称搜
    try:
        hits = market.search(w, limit=1)
        if hits:
            return hits[0].get("symbol")
    except Exception as exc:  # noqa: BLE001
        log.debug("chatbot 搜索失败 %s: %s", w, exc)
    return None


def _quote_line(sym: str) -> tuple[str, dict]:
    from ..sources import market
    q = market.get_quote(sym) or {}
    name = q.get("name") or market.display_name(sym)
    return f"{name} {sym.split('.')[0]}", q


# ---------------- 各命令 ----------------

def cmd_quote(sym: str) -> str:
    from . import box as box_svc
    from .flow import long_history

    title, q = _quote_line(sym)
    lines = [f"{title}",
             f"现价 {_num(q.get('price'))}  {_sign(q.get('pct_change'))}"]
    if q.get("volume") or q.get("turnover_rate"):
        lines.append(f"换手 {_num(q.get('turnover_rate'))}%  量比 {_num(q.get('vol_ratio'))}")
    try:
        bars = long_history(sym, 700)[-300:]
        bx = box_svc.adaptive(bars, q.get("price"))
        b = bx.get("analysis") or {}
        if b:
            lines.append(
                f"箱体 {b.get('bottom')} ~ {b.get('top')}"
                f"（位置 {_num(b.get('position_pct'), 1)}%）{b.get('shape', '')}")
        if bx.get("trustworthy") is False:
            lines.append("注：当前无可信箱体（可能在走趋势）")
    except Exception as exc:  # noqa: BLE001
        log.debug("chatbot 箱体失败 %s: %s", sym, exc)
    return _cut("\n".join(lines))


def cmd_anomaly(sym: str) -> str:
    from . import anomaly as anom
    try:
        d = anom.analyze(sym, with_news=False)   # 消息面慢，Chat 里不取
    except Exception as exc:  # noqa: BLE001
        return f"异动分析失败：{exc}"
    det, st, a = d.get("detect") or {}, d.get("state") or {}, d.get("attribution") or {}
    if not det.get("ok"):
        return f"{d.get('name')}：{det.get('reason') or '无法判断'}"
    lines = [f"{d.get('name')} {_sign(d.get('pct_change'))}",
             f"异动：{det.get('level_name')}"
             + (f"（{det.get('z_score')} 倍日常波动）" if det.get("z_score") else ""),
             f"大盘超额 {_sign(a.get('excess_vs_bench'))}  "
             f"板块超额 {_sign(a.get('excess_vs_peers'))}"]
    f = a.get("fund") or {}
    if f:
        lines.append(f"资金 {f.get('trend')}  20日 {_num((f.get('sum20') or 0) / 1e8)} 亿")
    lines.append(f"状态：{st.get('phase')} / {st.get('position_scope', '')}{st.get('position')}"
                 f" / {st.get('volume_price')}")
    cond = st.get("condition") or {}
    if cond.get("title"):
        lines.append(f"【{cond['title']}】")
        mean = (cond.get("meaning") or "").split("。")[0]
        lines.append(mean + "。")
    return _cut("\n".join(lines))


def cmd_plan(sym: str) -> str:
    from . import trades as trade_svc
    try:
        r = trade_svc.plan(sym)
    except Exception as exc:  # noqa: BLE001
        return f"方案生成失败：{exc}"
    if not r.get("ok"):
        return f"{sym}：{r.get('reason')}"
    b = r.get("recommended") or {}
    lines = [f"{r.get('name')} 现价 {r.get('price')}",
             f"风险预算 {_num(r.get('risk_amount'), 0)} 元"
             + (f"（总资金 {_num(r.get('capital'), 0)}）" if r.get("capital") else "")]
    for c in (r.get("candidates") or [])[:3]:
        if c.get("valid"):
            lines.append(f"{c['kind']} 止损 {c['stop']}（{c['stop_distance_pct']}%）"
                         f"→ {c['final_shares']} 股")
    if b.get("stop"):
        lines.append(f"推荐：止损 {b['stop']}，买 {b['final_shares']} 股，"
                     f"盈亏比 {b.get('rr')}")
    if b.get("rr") is not None and b["rr"] < 1.5:
        lines.append("⚠ 盈亏比低于 1.5，这位置赔率不划算")
    return _cut("\n".join(lines))


def cmd_positions() -> str:
    from . import trades as trade_svc
    rows = trade_svc.list_trades("open")
    if not rows:
        return "当前没有持仓记录。开仓后到「交易」页记录，这里就能查。"
    lines = [f"持仓 {len(rows)} 笔"]
    for r in rows[:6]:
        lines.append(
            f"{r.get('name')} {_num(r.get('shares'), 0)}股 "
            f"成本 {_num(r.get('entry_price'))} 现价 {_num(r.get('last_price'))} "
            f"{_sign(r.get('pnl_pct'))}")
        if r.get("stop_price"):
            lines.append(f"  止损 {r['stop_price']}"
                         + (f"  目标 {r['target_price']}" if r.get("target_price") else ""))
    return _cut("\n".join(lines))


def cmd_watchlist() -> str:
    from .. import db
    from ..sources import market
    rows = db.rows_to_dicts(db.query(
        "SELECT symbol, name FROM watchlist ORDER BY sort_order, id"))
    if not rows:
        return "自选股为空。"
    try:
        qs = market.get_quotes([r["symbol"] for r in rows])
    except Exception:  # noqa: BLE001
        qs = {}
    lines = [f"自选 {len(rows)} 只"]
    for r in rows[:12]:
        q = qs.get(r["symbol"]) or {}
        lines.append(f"{r.get('name') or r['symbol']} {_num(q.get('price'))} "
                     f"{_sign(q.get('pct_change'))}")
    return _cut("\n".join(lines))


# ---------------- 分发 ----------------

_HELP_WORDS = {"帮助", "help", "?", "？", "命令"}


def handle(text: str, username: str = "") -> str:
    """解析一条 Chat 消息，返回要回复的文本。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return HELP

    parts = t.split(" ")
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if head in _HELP_WORDS:
        return HELP
    if head in ("持仓", "仓位列表", "我的持仓"):
        return cmd_positions()
    if head in ("自选", "自选股", "watchlist"):
        return cmd_watchlist()

    # 「查/行情/异动/仓位 + 标的」
    if head in ("查", "行情", "报价") and rest:
        sym = _resolve(rest)
        return cmd_quote(sym) if sym else f"找不到标的：{rest}"
    if head in ("异动", "归因") and rest:
        sym = _resolve(rest)
        return cmd_anomaly(sym) if sym else f"找不到标的：{rest}"
    if head in ("仓位", "买多少", "方案") and rest:
        sym = _resolve(rest)
        return cmd_plan(sym) if sym else f"找不到标的：{rest}"

    # 只发一个代码/名称，默认当「查」
    sym = _resolve(t)
    if sym:
        return cmd_quote(sym)

    return "没看懂这条命令。\n\n" + HELP
