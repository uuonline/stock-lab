"""HTTP API 路由层。"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..services import ai as ai_svc
from ..services import alerts as alert_svc
from ..services import backtest as bt_svc
from ..services import analogs as analogs_svc
from ..services import anomaly as anomaly_svc
from ..services import box as box_svc
from ..services import flow as flow_svc
from ..services import indicators as ta
from ..services import notify as notify_svc
from ..services import ordersheet as sheet_svc
from ..services import trades as trade_svc
from ..services import quote as quote_svc
from ..services import screener as screen_svc
from ..sources import alphavantage as alphavantage_src
from ..sources import market
from ..sources.base import FetchError, breakers_status
from ..symbols import SymbolError, asset_type, board_of, display_name, normalize

log = logging.getLogger("stocklab.api")
router = APIRouter(prefix="/api")


# ---------------- 通用 ----------------

@router.get("/health")
def health() -> dict:
    return {"ok": True, "app": settings.app_name, "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@router.get("/status")
def status() -> dict:
    """系统总览：数据源、快照、调度、AI、通知渠道。"""
    from ..tasks import scheduler as sched
    return {
        "market": quote_svc.market_status(),
        "snapshot": market.snapshot_meta(),
        "db": {
            "path": str(settings.db_path),
            "kline_rows": (db.query_one("SELECT COUNT(*) AS c FROM kline_daily") or {"c": 0})["c"],
            "watch_count": (db.query_one("SELECT COUNT(*) AS c FROM watchlist") or {"c": 0})["c"],
            "alert_count": (db.query_one("SELECT COUNT(*) AS c FROM alerts WHERE enabled=1") or {"c": 0})["c"],
        },
        "scheduler": {
            "enabled": settings.scheduler_enabled,
            "jobs": sched.jobs(),
        },
        "ai": {
            "enabled": settings.ai_enabled and bool(settings.ai_api_key),
            "model": settings.ai_model,
            "base_url": settings.ai_base_url,
        },
        "notify": notify_svc.status(),
        "source_order": settings.source_order,
        "breakers": breakers_status(),
        "alphavantage": alphavantage_src.status(),
    }


# ---------------- 行情 ----------------

@router.get("/market/overview")
def market_overview() -> dict:
    # 注意：这里绝不做同步抓取。首次启动数据库为空时，同步抓全市场要 1-2 分钟，
    # 会把首页卡死（实测浏览器直接超时）。ensure_snapshot 已改为后台异步刷新。
    market.ensure_snapshot("a_share")
    return {
        "status": quote_svc.market_status(),
        "indices": quote_svc.indices_overview(),
        "breadth": quote_svc.market_breadth(),
        "snapshot": market.snapshot_meta(),
        "snapshot_state": market.snapshot_status(),
    }


@router.get("/snapshot/status")
def snapshot_status() -> dict:
    """快照就绪状态，供前端轮询提示用。"""
    return market.snapshot_status()


@router.get("/market/movers")
def market_movers(kind: str = "up", limit: int = 10) -> dict:
    market.ensure_snapshot("a_share")
    rows = quote_svc.movers(kind, min(limit, 100))
    for r in rows:
        r["board"] = board_of(r["symbol"])
    return {"kind": kind, "rows": rows}


@router.get("/quote")
def get_quotes(symbols: str = Query(..., description="逗号分隔，如 600519.SH,000001.SZ")) -> dict:
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not syms:
        raise HTTPException(400, "symbols 不能为空")
    if len(syms) > 200:
        raise HTTPException(400, "单次最多查询 200 个标的")
    try:
        qs = market.get_quotes(syms)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"quotes": qs}


@router.get("/quote/{symbol}")
def get_quote(symbol: str) -> dict:
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        # 详情页单只标的，做字段补充以拿到主力净流入等东财独有字段
        qs = market.get_quotes([sym], enrich=True)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    q = qs.get(sym)
    if not q:
        raise HTTPException(404, f"未找到行情: {sym}")
    return q


@router.get("/kline/{symbol}")
def get_kline(
    symbol: str,
    period: str = Query("day", pattern="^(1m|5m|15m|30m|60m|day|week|month)$"),
    limit: int = Query(320, ge=10, le=2000),
    adjust: int = Query(1, ge=0, le=2),
    indicators: int = Query(1, ge=0, le=1),
) -> dict:
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        bars = market.get_kline(sym, period, limit, adjust)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not bars:
        raise HTTPException(404, f"无 K线数据: {sym}")
    resp: dict[str, Any] = {"symbol": sym, "period": period, "bars": bars, "count": len(bars)}
    if indicators and period in ("day", "week", "month"):
        # 只算一次：latest_snapshot 复用同一份指标，别再重算一遍
        ind = ta.compute_all(bars)
        resp["indicators"] = ind
        resp["snapshot"] = ta.latest_snapshot(bars, ind)
        # 箱体分析随 K线一起返回，前端可直接画在图上
        try:
            q = market.get_quotes([sym]).get(sym) or {}
            resp["box"] = box_svc.analyze_multi(bars, q.get("price"))
        except Exception as exc:  # noqa: BLE001
            log.debug("箱体分析失败 %s: %s", sym, exc)
    return resp


@router.get("/box/{symbol}")
def get_box(
    symbol: str,
    window: int = Query(60, ge=20, le=250),
    multi: int = Query(0, ge=0, le=1),
    adaptive: int = Query(0, ge=0, le=1),
) -> dict:
    """箱体分析。

    multi=1    返回 30/60/120/250 四个窗口的对比，方便判断该看哪个周期。
    adaptive=1 按该股自身的波段节奏推荐窗口（固定窗口用同一把尺子量所有股票，
               并不贴合个股节奏）。需要更长历史，所以多取一些 K线。
    """
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 自适应要扫到 250 天窗口 + 测节奏，300 根不够
    need = 800 if adaptive else 300
    try:
        bars = market.get_kline(sym, "day", need)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not bars or len(bars) < 20:
        raise HTTPException(404, "K线数据不足，无法做箱体分析")

    q = market.get_quotes([sym]).get(sym) or {}
    price = q.get("price")
    name = q.get("name") or display_name(sym)

    if adaptive:
        return {"symbol": sym, "name": name, **box_svc.adaptive(bars, price)}

    if multi:
        res = box_svc.analyze_multi(bars, price)
        return {"symbol": sym, "name": name, **res}

    r = box_svc.analyze(bars, window, price)
    if not r:
        raise HTTPException(404, "数据不足，无法识别箱体")
    return {"symbol": sym, "name": name, **r}


@router.get("/trends/{symbol}")
def get_trends(symbol: str, ndays: int = Query(1, ge=1, le=5)) -> dict:
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"symbol": sym, "trends": market.get_trends(sym, ndays)}


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=50)) -> dict:
    return {"query": q, "results": market.search(q, limit)}


# ---------------- 自选股 ----------------

class WatchAdd(BaseModel):
    symbol: str
    group: str = "默认分组"
    note: str = ""


@router.get("/watchlist")
def get_watchlist(
    group: str | None = None,
    with_tech: int = Query(0, ge=0, le=1),
) -> dict:
    rows = quote_svc.watchlist_with_quotes(group, with_tech=bool(with_tech))
    return {"rows": rows, "groups": quote_svc.list_groups(), "count": len(rows)}


@router.post("/watchlist")
def add_watch(body: WatchAdd) -> dict:
    try:
        return quote_svc.add_watch(body.symbol, body.group, body.note)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/watchlist/{symbol}")
def del_watch(symbol: str, group: str | None = None) -> dict:
    try:
        quote_svc.remove_watch(symbol, group)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/watchlist/reorder")
def reorder_watch(group: str = Body(...), symbols: list[str] = Body(...)) -> dict:
    quote_svc.reorder_watch(group, symbols)
    return {"ok": True}


# ---------------- 基本面 ----------------

@router.get("/fundamentals/{symbol}")
def get_fundamentals(symbol: str, force: int = Query(0, ge=0, le=1)) -> dict:
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        data = market.get_fundamentals(sym, force=bool(force))
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        q = market.get_quotes([sym]).get(sym) or {}
    except Exception:  # noqa: BLE001
        q = {}
    pe = q.get("pe_ttm") or q.get("pe")
    pb = q.get("pb")
    bps = (data.get("latest") or {}).get("bps")
    pe_est = None
    if bps and pb:
        pe_est = None
    valuation = {
        "pe_ttm": pe, "pb": pb,
        "market_cap": q.get("market_cap"), "float_cap": q.get("float_cap"),
        "dividend_hint": None,
    }
    if pe_est:
        valuation["pe_from_pb"] = pe_est
    return {**data, "name": q.get("name") or display_name(sym), "valuation": valuation}


@router.get("/indicators/{symbol}")
def get_indicators(symbol: str, limit: int = Query(260, ge=60, le=1000)) -> dict:
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        bars = market.get_kline(sym, "day", limit)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not bars:
        raise HTTPException(404, "无数据")
    ind = ta.compute_all(bars)
    return {
        "symbol": sym,
        "name": (market.get_quotes([sym]).get(sym) or {}).get("name") or display_name(sym),
        "bars": len(bars),
        "snapshot": ta.latest_snapshot(bars, ind),
        "indicators": ind,
    }


# ---------------- 选股 ----------------

class ScreenRequest(BaseModel):
    conditions: list[dict] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    tech_params: dict = Field(default_factory=dict)
    kind: str = "a_share"
    exclude_st: bool = True
    exclude_new: bool = True
    order: str = "amount DESC"
    limit: int = Field(50, ge=1, le=200)
    # 技术面过滤需逐只拉 K线，限制扫描数量以免请求超时
    tech_scan_limit: int = Field(150, ge=20, le=600)


@router.get("/screen/presets")
def screen_presets() -> dict:
    return {
        "presets": screen_svc.presets(),
        "fields": screen_svc.SNAPSHOT_FIELDS,
        "tech_conditions": screen_svc.TECH_CONDITIONS,
    }


ALLOWED_ORDERS = (
    "amount DESC", "pct_change DESC", "pct_change ASC",
    "turnover_rate DESC", "market_cap DESC", "pe ASC", "pb ASC",
)


@router.post("/screen")
def run_screen(req: ScreenRequest) -> dict:
    market.ensure_snapshot("a_share")
    order_warning = None
    if req.order not in ALLOWED_ORDERS:
        # 排序字段走白名单拼接 SQL，非法值一律回落 —— 但要告诉用户，
        # 否则他会以为按自己的字段排序了
        order_warning = (
            f"排序字段 '{req.order}' 不受支持，已回落到按成交额排序；"
            f"可用值: {', '.join(ALLOWED_ORDERS)}"
        )
        req.order = "amount DESC"
    try:
        result = screen_svc.screen(
            req.conditions, req.tech, req.tech_params, req.kind,
            req.exclude_st, req.exclude_new, req.order, req.limit,
            req.tech_scan_limit,
        )
        if order_warning:
            result.setdefault("warnings", []).insert(0, order_warning)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("选股失败")
        raise HTTPException(500, f"选股失败: {exc}") from exc


class SaveScreen(BaseModel):
    name: str
    conditions: list[dict] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)


@router.get("/screen/saved")
def list_saved_screens() -> dict:
    return {"screens": screen_svc.list_screens()}


@router.post("/screen/saved")
def save_screen(body: SaveScreen) -> dict:
    sid = screen_svc.save_screen(body.name, body.conditions, body.tech)
    return {"ok": True, "id": sid}


@router.delete("/screen/saved/{sid}")
def delete_saved_screen(sid: int) -> dict:
    screen_svc.delete_screen(sid)
    return {"ok": True}


# ---------------- 回测 ----------------

@router.get("/backtest/strategies")
def backtest_strategies() -> dict:
    return {
        "strategies": [
            {"key": k, "name": v["name"], "desc": v["desc"], "params": v["params"]}
            for k, v in bt_svc.STRATEGIES.items()
        ]
    }


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "ma_cross"
    params: dict = Field(default_factory=dict)
    initial_cash: float = Field(100000.0, gt=0)
    days: int = Field(500, ge=60, le=3000)
    position_size: float = Field(1.0, gt=0, le=1)
    allow_fractional: bool = False
    compare_all: bool = False
    save: bool = True


@router.post("/backtest")
def run_backtest(req: BacktestRequest) -> dict:
    try:
        sym = normalize(req.symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        bars = market.get_kline(sym, "day", req.days)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not bars or len(bars) < 30:
        raise HTTPException(400, "K线数据不足，无法回测")

    name = (market.get_quotes([sym]).get(sym) or {}).get("name") or display_name(sym)
    cost = bt_svc.CostModel(
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage_rate=settings.slippage_rate,
        is_sh=sym.endswith(".SH"),
    )

    try:
        result = bt_svc.run(
            bars, req.strategy, req.params, req.initial_cash, cost,
            req.position_size, sym, req.allow_fractional,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    payload: dict[str, Any] = {
        "symbol": sym, "name": name,
        "strategy": req.strategy,
        "strategy_name": bt_svc.STRATEGIES[req.strategy]["name"],
        "params": {**bt_svc.STRATEGIES[req.strategy]["params"], **(req.params or {})},
        "metrics": result.metrics,
        "equity": result.equity,
        "trades": result.trades,
        "period": {"start": bars[0]["date"], "end": bars[-1]["date"], "bars": len(bars)},
    }
    if req.compare_all:
        payload["comparison"] = bt_svc.compare_strategies(
            bars, req.initial_cash, sym, allow_fractional=req.allow_fractional
        )

    if req.save:
        import json
        try:
            rid = db.execute(
                "INSERT INTO backtest_runs(symbol,strategy,params,start_date,end_date,metrics,equity,trades) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    sym, req.strategy, json.dumps(payload["params"], ensure_ascii=False),
                    bars[0]["date"], bars[-1]["date"],
                    json.dumps(result.metrics, ensure_ascii=False),
                    json.dumps(result.equity[-250:], ensure_ascii=False),
                    json.dumps(result.trades[-100:], ensure_ascii=False),
                ),
            )
            payload["run_id"] = rid
        except Exception as exc:  # noqa: BLE001
            log.warning("回测记录保存失败: %s", exc)

    return payload


@router.get("/backtest/history")
def backtest_history(limit: int = Query(20, ge=1, le=100)) -> dict:
    rows = db.rows_to_dicts(
        db.query(
            "SELECT id,symbol,strategy,start_date,end_date,metrics,created_at "
            "FROM backtest_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )
    import json
    for r in rows:
        try:
            r["metrics"] = json.loads(r.get("metrics") or "{}")
        except (ValueError, TypeError):
            r["metrics"] = {}
    return {"runs": rows}


# ---------------- AI 简报 ----------------

class AIRequest(BaseModel):
    symbol: str = ""
    scope: str = "single"   # single | market
    save: bool = True


@router.post("/ai/report")
def ai_report(req: AIRequest) -> dict:
    if req.scope == "market":
        content = ai_svc.market_overview_report()
        return {
            "symbol": "", "name": "大盘综述", "content": content,
            "used_ai": False, "model": "local-rule-engine",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    if not req.symbol:
        raise HTTPException(400, "请提供 symbol")
    try:
        sym = normalize(req.symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return ai_svc.generate_report(sym, save=req.save)
    except Exception as exc:  # noqa: BLE001
        log.exception("生成报告失败")
        raise HTTPException(500, f"生成报告失败: {exc}") from exc


@router.get("/ai/reports")
def ai_reports(limit: int = Query(30, ge=1, le=100)) -> dict:
    return {"reports": ai_svc.list_reports(limit)}


@router.get("/ai/reports/{rid}")
def ai_report_detail(rid: int) -> dict:
    r = ai_svc.get_report(rid)
    if not r:
        raise HTTPException(404, "报告不存在")
    return r


# ---------------- 提醒 ----------------

class AlertAdd(BaseModel):
    symbol: str
    rule_type: str
    params: dict = Field(default_factory=dict)
    message: str = ""
    cooldown: int = Field(1800, ge=0, le=86400)


@router.get("/alerts")
def list_alerts() -> dict:
    return {
        "alerts": alert_svc.list_alerts(),
        "rule_types": alert_svc.RULE_TYPES,
        "events": alert_svc.recent_events(30),
    }


@router.post("/alerts")
def add_alert(body: AlertAdd) -> dict:
    try:
        aid = alert_svc.add_alert(
            body.symbol, body.rule_type, body.params, message=body.message,
            cooldown=body.cooldown,
        )
    except (SymbolError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "id": aid}


@router.post("/alerts/{aid}/toggle")
def toggle_alert(aid: int, enabled: bool = Body(..., embed=True)) -> dict:
    alert_svc.toggle_alert(aid, enabled)
    return {"ok": True}


@router.delete("/alerts/{aid}")
def delete_alert(aid: int) -> dict:
    alert_svc.delete_alert(aid)
    return {"ok": True}


@router.post("/alerts/check")
def check_alerts_now() -> dict:
    fired = alert_svc.check_all_quotes()
    return {"fired": fired, "count": len(fired)}


@router.post("/alerts/test-notify")
def test_notify() -> dict:
    results = alert_svc.test_notify()
    if not results:
        return {"ok": False, "detail": "未配置任何推送渠道，请在 .env 中填写", "results": {}}
    return {"ok": any(results.values()), "results": results}


@router.get("/alerts/events")
def alert_events(limit: int = Query(50, ge=1, le=200)) -> dict:
    return {"events": alert_svc.recent_events(limit)}


# ---------------- 任务与维护 ----------------

@router.get("/tasks/logs")
def task_logs(limit: int = Query(50, ge=1, le=200)) -> dict:
    from ..tasks import scheduler as sched
    return {"logs": sched.recent_logs(limit), "jobs": sched.jobs()}


@router.post("/tasks/run")
def run_task(task: str = Body(..., embed=True)) -> dict:
    from ..tasks import scheduler as sched
    try:
        msg = sched.run_now(task)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": msg}


@router.post("/maintenance/refresh-snapshot")
def refresh_snapshot(kind: str = Body("a_share", embed=True)) -> dict:
    if kind not in market.SNAPSHOT_KINDS:
        raise HTTPException(400, f"不支持的市场: {kind}")
    try:
        n = market.refresh_snapshot(kind)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "count": n, "kind": kind}


@router.get("/selftest")
def selftest(include_optional: int = Query(1, ge=0, le=1)) -> dict:
    """部署自检：验证数据源、数据库、指标、回测、调度是否在这台机器上真的可用。"""
    from ..services import selftest as st

    return st.run_all(include_optional=bool(include_optional))


@router.get("/flow/{symbol}")
def get_flow(symbol: str) -> dict:
    """AI 全流程分析：6 步 13 提示词。

    能算的用真实数据算，算不出的明确标注「本系统无此数据」——
    不会为了让每项都有答案而编造。
    """
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return flow_svc.build(sym)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/flow/{symbol}/pack")
def get_flow_pack(symbol: str) -> dict:
    """把 13 个提示词原文 + 本系统的真实数据拼成可直接复制的文本。

    给"想拿去外部 AI 再跑一遍"的用户用：提示词不变，
    但把真实数据一起带上，外部 AI 就不需要（也不会）编造这些数字。
    """
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        text = flow_svc.data_pack(sym)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"symbol": sym, "chars": len(text), "text": text}


@router.get("/anomaly/{symbol}")
def get_anomaly(symbol: str, news: int = Query(1, ge=0, le=1)) -> dict:
    """异动归因：发生了什么 / 正在发生什么 / 什么条件下会怎样。

    用条件树而不是加权评分 —— 同样的形态在不同资金和位置下含义相反。
    """
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return anomaly_svc.analyze(sym, with_news=bool(news))
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/analogs/{symbol}")
def get_analogs(symbol: str) -> dict:
    """历史类比：这种情形以前发生过什么。

    回答「历史上出现类似异动后，5/10/20 个交易日实际发生了什么」——
    是历史统计，不是预测。同时给出独立事件数、样本内外对比和最坏情况。
    """
    try:
        sym = normalize(symbol)
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        from ..services import flow as flow_svc
        bars = flow_svc.long_history(sym, 1300)
    except FetchError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not bars:
        raise HTTPException(404, f"无 K线数据: {sym}")
    try:
        return analogs_svc.analyze(sym, bars)
    except Exception as exc:  # noqa: BLE001
        log.warning("历史类比失败 %s: %s", sym, exc)
        return {"ok": False, "reason": f"历史类比计算失败: {exc}"}


# ---------------- 交易日志与仓位计算 ----------------

class TradeIn(BaseModel):
    symbol: str
    entry_price: float = Field(gt=0)
    shares: float = Field(default=0, ge=0)
    stop_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    reason: str = ""
    note: str = ""
    entry_date: str | None = None
    name: str = ""
    auto_alerts: bool = True     # 按止损/目标价自动建提醒


class CloseIn(BaseModel):
    exit_price: float = Field(gt=0)
    exit_date: str | None = None
    note: str = ""


class CalcIn(BaseModel):
    capital: float = Field(gt=0)
    risk_pct: float = Field(gt=0, le=100)
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    max_position_pct: float = Field(default=30.0, gt=0, le=100)
    available_cash: float | None = Field(default=None, ge=0)


class SettingsIn(BaseModel):
    available_cash: float | None = Field(default=None, ge=0)


@router.get("/trades/settings")
def trades_settings() -> dict:
    return trade_svc.get_settings()


@router.post("/trades/settings")
def trades_settings_set(body: SettingsIn) -> dict:
    """存可用资金 —— 用来算「可买多少股」，和券商 App 的口径一致。"""
    r = trade_svc.set_settings(body.available_cash)
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "参数无效")
    return r


@router.post("/trades/calc")
def trades_calc(body: CalcIn) -> dict:
    """由「能亏多少」反推「该买多少」。

    顺序很重要：先定单笔风险，再反推股数。
    先决定买多少再看能亏多少，几乎必然失控。
    """
    # 未显式传可用资金时，用「交易」页存过的那个
    cash = body.available_cash
    if cash is None:
        cash = (trade_svc.get_settings() or {}).get("available_cash")
    r = trade_svc.calc_position(
        body.capital, body.risk_pct, body.entry, body.stop,
        body.target, body.max_position_pct, cash,
    )
    # 参数语义无效（比如止损高于入场价）属于客户端错误，应当 400，
    # 不能返回 200 + ok:false —— 那样调用方按状态码判断就会误以为成功。
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "参数无效")
    return r


@router.get("/trades")
def list_trades(status: str | None = Query(None, pattern="^(open|closed)$"),
                limit: int = Query(200, ge=1, le=1000)) -> dict:
    return {"rows": trade_svc.list_trades(status, limit)}


@router.get("/trades/stats")
def trades_stats() -> dict:
    """按入场理由分组的复盘统计。

    只看总胜率意义不大，真正要回答的是「哪一类入场理由对我有效」。
    """
    return trade_svc.stats()


@router.post("/trades")
def create_trade(body: TradeIn) -> dict:
    try:
        return trade_svc.open_trade(
            body.symbol, body.entry_price, body.shares, body.stop_price,
            body.target_price, body.reason, body.note, body.entry_date, body.name,
            auto_alerts=body.auto_alerts,
        )
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/trades/{trade_id}/close")
def close_trade(trade_id: int, body: CloseIn) -> dict:
    r = trade_svc.close_trade(trade_id, body.exit_price, body.exit_date, body.note)
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "平仓失败")
    return r


@router.delete("/trades/{trade_id}")
def delete_trade(trade_id: int) -> dict:
    r = trade_svc.delete_trade(trade_id)
    if not r.get("ok"):
        raise HTTPException(404, "找不到该交易")
    return r


@router.get("/order-sheet")
def order_sheet(watchlist: int = Query(1, ge=0, le=1)) -> dict:
    """下单参数清单：把系统里已确定的参数排成"照着填"的形式。

    只为减少人肉转录的错误 —— **不做新计算**。
    本系统不接券商交易接口（那需要交易密码，也没有公开接口），
    下单动作始终由用户在自己的 App 里完成。
    """
    return sheet_svc.build(include_watchlist=bool(watchlist))
