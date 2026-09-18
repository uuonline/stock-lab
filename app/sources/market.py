"""统一行情门面：多源容错 + 本地落库 + 标的解析。

策略：
  * 实时行情 —— 按 settings.source_order 依次尝试，缺失的标的由后续源补齐
  * K线      —— 东财优先（字段全、含成交额），腾讯兜底（含港股/分钟线）
  * 全市场    —— 仅东财提供，抓取后写入 SQLite 供选股与搜索
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Iterable

from .. import db
from ..config import settings
from ..symbols import (
    INDEX_CODES, asset_type, board_of, display_name, normalize, SymbolError,
)
from . import alphavantage, eastmoney, sina, tencent
from .base import BREAKERS, FetchError, cache

log = logging.getLogger("stocklab.market")

QUOTE_KEYS = (
    "price", "pct_change", "change", "volume", "amount", "amplitude",
    "turnover_rate", "pe", "pe_ttm", "pb", "vol_ratio", "high", "low",
    "open", "prev_close", "market_cap", "float_cap", "main_net_inflow",
    "main_net_pct", "limit_up", "limit_down",
)

# 主力资金类字段只有东财提供，腾讯/新浪拿不到 —— 需要时按需补充
_ENRICH_KEYS = ("main_net_inflow", "main_net_pct")


def _merge_quote(base: dict, extra: dict) -> dict:
    """用 extra 补齐 base 中缺失的字段。"""
    for k in QUOTE_KEYS:
        if base.get(k) is None and extra.get(k) is not None:
            base[k] = extra[k]
    return base


SOURCES = {
    "eastmoney": eastmoney,
    "tencent": tencent,
    "sina": sina,
    # Alpha Vantage 放最后：它一次只能查一只、额度很紧，
    # 只在前三个源都失败时兜底（额度保护见 alphavantage.py）
    "alphavantage": alphavantage,
}


def get_quotes(
    symbols: Iterable[str],
    prefer: str | None = None,
    enrich: bool = False,
) -> dict[str, dict]:
    """批量实时行情，多源容错。

    实测成功率（本机 12 次采样）：
        腾讯 100%  |  新浪 100%  |  东财 push2  42%
    所以默认把腾讯放在首位 —— 东财字段虽全，但拿不到数据时字段全也没意义。
    东财缺的只有「主力净流入」等少数字段，需要时用 enrich=True 做一次
    best-effort 补充请求（失败不影响主流程）。
    """
    wanted: list[str] = []
    for s in symbols:
        try:
            sym = normalize(s)
        except SymbolError:
            continue
        if sym not in wanted:
            wanted.append(sym)
    if not wanted:
        return {}

    order = ([prefer] if prefer else []) + [s for s in settings.source_order if s != prefer]
    result: dict[str, dict] = {}
    errors: list[str] = []
    # 是否至少有一个源「成功响应过」（哪怕它说没有这只标的）。
    # 区分这两种情况很重要：
    #   所有源都报错        -> 503 服务不可用，让用户稍后重试
    #   源正常但查无此标的  -> 404 标的不存在
    # 混在一起会把「代码打错了」误报成「服务挂了」。
    responded = False

    for src in order:
        missing = [s for s in wanted if s not in result]
        if not missing:
            break
        mod = SOURCES.get(src)
        if mod is None:
            continue
        breaker = BREAKERS.get(src)
        if breaker is not None and not breaker.allow():
            log.debug("行情源 %s 处于熔断期，跳过", src)
            errors.append(f"{src}: 熔断中")
            continue
        try:
            got = mod.quotes(missing)
            responded = True
        except FetchError as exc:
            # 网络/上游问题 —— 预期内，交给下一个源
            if breaker is not None:
                breaker.record_failure()
            errors.append(f"{src}: {exc}")
            log.debug("行情源 %s 失败: %s", src, exc)
            continue
        except (TypeError, AttributeError, NameError, ImportError) as exc:
            # 编程错误：绝不能被兜底逻辑悄悄吞掉，否则某个源会永久失效
            # 而系统看起来「一切正常」—— 这个坑实际踩过一次。
            log.error("行情源 %s 存在代码错误（该源已被跳过，请修复）: %s", src, exc, exc_info=True)
            errors.append(f"{src}: 代码错误 {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            if breaker is not None:
                breaker.record_failure()
            errors.append(f"{src}: {exc}")
            log.warning("行情源 %s 异常: %s", src, exc)
            continue
        if breaker is not None:
            breaker.record_success()
        for sym, q in got.items():
            if sym in result:
                _merge_quote(result[sym], q)
            else:
                q.setdefault("_source", src)
                result[sym] = q
        # 主源已覆盖全部标的就停，不再无谓地打后面的源
        if len(result) == len(wanted):
            break

    # 字段补充：只在主源已有数据、且确实有字段为空时才发起。
    # 预算受控 —— 补充字段（主力净流入）只影响展示丰富度，
    # 绝不能为了它把详情页拖慢几秒。
    if enrich and result:
        deadline = time.monotonic() + settings.enrich_budget
        for src in settings.enrich_sources:
            if time.monotonic() >= deadline:
                log.debug("补充字段超预算，跳过")
                break
            mod = SOURCES.get(src)
            if mod is None:
                continue
            breaker = BREAKERS.get(src)
            if breaker is not None and not breaker.allow():
                continue
            need = [
                sym for sym, q in result.items()
                if any(q.get(k) is None for k in _ENRICH_KEYS)
            ]
            if not need:
                break
            try:
                extra = mod.quotes(
                    need,
                    retries=settings.enrich_retries,
                    timeout=max(1.0, settings.enrich_budget),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("补充源 %s 失败（忽略）: %s", src, exc)
                if breaker is not None:
                    breaker.record_failure()
                continue
            for sym, q in extra.items():
                if sym in result:
                    _merge_quote(result[sym], q)

    for sym in wanted:
        q = result.get(sym)
        if q is None:
            continue
        q.setdefault("symbol", sym)
        q["name"] = q.get("name") or display_name(sym)
        q["asset_type"] = asset_type(sym)
        q["board"] = board_of(sym)
    # 只有「一个源都没成功响应」才算服务故障；否则就是单纯查无此标的
    if errors and not result and not responded:
        raise FetchError("全部行情源失败: " + "; ".join(errors))
    if errors and not result:
        log.debug("标的无行情（源已响应，非故障）: %s", "; ".join(errors))
    return result


def get_quote(symbol: str, enrich: bool = True) -> dict | None:
    """单只行情。默认做字段补充（单只标的，补充成本可接受）。"""
    return get_quotes([symbol], enrich=enrich).get(normalize(symbol))


def _kline_needs_amount(symbol: str) -> bool:
    return not symbol.endswith(".HK")


def get_kline(
    symbol: str,
    period: str = "day",
    limit: int = 320,
    adjust: int = 1,
    use_local: bool = True,
) -> list[dict]:
    """K线，东财优先腾讯兜底。"""
    sym = normalize(symbol)
    ck = f"K|{sym}|{period}|{limit}|{adjust}"
    hit = cache.get(ck)
    if hit is not None:
        return hit

    bars: list[dict] = []
    last_err: Exception | None = None

    # 数据源顺序：东财（字段全）→ 腾讯（稳定、支持港股）→ 新浪（兜底）
    # 实测多个源会被单方面限流（腾讯 fqkline 返 501、东财 push2his 整族被封），
    # 所以链路要够长。Alpha Vantage 仅支持日线且额度紧，放最后。
    for src in ("eastmoney", "tencent", "sina", "alphavantage"):
        breaker = BREAKERS.get(src)
        if breaker is not None and not breaker.allow():
            log.debug("K线源 %s 处于熔断期，跳过", src)
            last_err = last_err or FetchError(f"{src} 熔断中")
            continue
        try:
            if src == "eastmoney":
                bars = eastmoney.kline(sym, period, limit, adjust)
            elif src == "tencent":
                bars = tencent.kline(
                    sym, period, limit, "qfq" if adjust == 1 else ("bfq" if adjust == 0 else "hfq")
                )
            elif src == "sina":
                bars = sina.kline(sym, period, limit)
            else:
                # Alpha Vantage 只支持日线且不支持港股，不满足条件就跳过
                if period not in ("day", "daily") or not alphavantage.is_supported(sym):
                    continue
                bars = alphavantage.kline(sym, period, limit)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            bars = []
            if breaker is not None:
                breaker.record_failure()
            continue
        if bars:
            if breaker is not None:
                breaker.record_success()
            break

    if not bars and use_local and period == "day":
        local = db.load_kline(sym)
        if local:
            bars = local[-limit:]
            log.info("K线回落本地库: %s (%d 条)", sym, len(bars))

    if not bars and last_err is not None:
        raise FetchError(f"K线获取失败 {sym}: {last_err}")

    if bars and period == "day":
        try:
            db.save_kline(sym, bars)
        except Exception as exc:  # noqa: BLE001
            log.warning("K线落库失败 %s: %s", sym, exc)

    cache.set(ck, bars, settings.kline_cache_ttl)
    return bars


def get_trends(symbol: str, ndays: int = 1) -> list[dict]:
    """当日分时。东财优先（含均价线），腾讯兜底。

    实测东财 trends2 在 push2his 整族被封时仍可经 push2delay 取得；
    若两个域名族都不可用，则用腾讯 minute/query。
    """
    sym = normalize(symbol)
    ck = f"T|{sym}|{ndays}"
    hit = cache.get(ck)
    if hit is not None:
        return hit

    rows: list[dict] = []
    for src in ("eastmoney", "tencent"):
        breaker = BREAKERS.get(src)
        if breaker is not None and not breaker.allow():
            continue
        try:
            rows = (eastmoney.trends(sym, ndays) if src == "eastmoney"
                    else tencent.minutes(sym))
        except Exception as exc:  # noqa: BLE001
            log.debug("分时源 %s 失败 %s: %s", src, sym, exc)
            rows = []
            if breaker is not None:
                breaker.record_failure()
            continue
        if rows:
            if breaker is not None:
                breaker.record_success()
            break

    if rows:
        rows = _trim_session(sym, rows)
        rows = _fill_avg(rows)
        cache.set(ck, rows, 30)
    return rows


# 各市场正常交易时段的收盘时间（分时图不应画出盘后数据）
_SESSION_CLOSE = {"SH": "15:00", "SZ": "15:00", "BJ": "15:00", "HK": "16:00"}


def _trim_session(symbol: str, rows: list[dict]) -> list[dict]:
    """裁掉收盘之后的数据点。

    腾讯的 minute/query 会一直给到 15:30（盘后固定价格交易等），
    A股走势图若照单全收，15:00 之后会多出一截平线。东财本身只给到 15:00，
    所以这个裁剪只对兜底源生效。
    """
    if not rows:
        return rows
    close = _SESSION_CLOSE.get(symbol.rpartition(".")[2])
    if not close:
        return rows
    out = []
    for r in rows:
        t = str(r.get("time") or "")
        hhmm = t[-5:] if len(t) >= 5 else ""
        if len(hhmm) == 5 and hhmm[2] == ":" and hhmm > close:
            continue
        out.append(r)
    return out or rows


def _fill_avg(rows: list[dict]) -> list[dict]:
    """补齐均价线（VWAP）。

    东财的 trends2 自带均价字段，腾讯的 minute/query **没有**。
    如果不管，回退到腾讯时分时图就少一条均价线 —— 而均价是分时图的核心
    （判断"价格在均线上方还是下方"全靠它）。
    所以缺的时候自行计算：累计成交额 ÷ 累计成交量。
    """
    cum_amt = 0.0
    cum_vol = 0.0          # 股
    for i, r in enumerate(rows):
        vol = r.get("volume") or 0.0        # 手
        amt = r.get("amount") or 0.0        # 元
        cum_vol += vol * 100.0
        cum_amt += amt
        if r.get("avg") is not None:
            continue
        price = r.get("price")
        if i == 0 or cum_vol <= 0 or cum_amt <= 0:
            # 第一分钟没有历史累计，均价就是当时价格
            r["avg"] = price
        else:
            vwap = cum_amt / cum_vol
            # 数据异常（偏离价格超过 20%）时退回价格，避免画出明显错误的均线
            if price and (vwap < price * 0.8 or vwap > price * 1.2):
                r["avg"] = price
            else:
                r["avg"] = round(vwap, 3)
    return rows


# ---------------- 全市场快照 ----------------

SNAPSHOT_KINDS = ("a_share", "hk", "etf", "index", "sh", "sz", "bj")


def refresh_snapshot(kind: str = "a_share") -> int:
    """抓全市场快照并写入 market_snapshot / instruments。"""
    stats: dict[str, Any] = {}
    rows: list[dict] = []
    # 全市场列表原本只有东财提供，而东财会整站封 IP（实测所有分片同时断开）。
    # 新浪的列表接口此时仍可用（5564 只 A股，含 PE/PB/市值/换手率），
    # 因此作为兜底 —— 否则选股功能会在东财被封期间彻底不可用。
    try:
        rows = eastmoney.full_snapshot(kind, stats=stats)
        stats["source"] = "eastmoney"
    except Exception as exc:  # noqa: BLE001
        if kind not in sina.LIST_NODES or sina.LIST_NODES.get(kind) is None:
            raise
        log.warning("东财快照失败（%s），改用新浪兜底", exc)
        rows = sina.full_market_list(kind, stats=stats)
    if not rows:
        raise FetchError(f"快照为空: {kind}")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    snap_rows = []
    inst_rows = []
    for q in rows:
        sym = q.get("symbol")
        if not sym:
            continue
        at = asset_type(sym)
        if kind == "index":
            at = "index"
        elif kind == "etf":
            at = "etf"
        elif kind == "hk":
            at = "hk"
        snap_rows.append(
            (
                sym, q.get("name"), at, q.get("price"), q.get("pct_change"),
                q.get("change"), q.get("volume"), q.get("amount"),
                q.get("turnover_rate"), q.get("pe_ttm") or q.get("pe"), q.get("pb"),
                q.get("market_cap"), q.get("float_cap"), q.get("amplitude"),
                q.get("high"), q.get("low"), q.get("open"), q.get("prev_close"), ts,
            )
        )
        inst_rows.append((sym, q.get("name"), at, sym.rpartition(".")[2], None, ts))

    db.executemany(
        "INSERT INTO market_snapshot(symbol,name,asset_type,price,pct_change,change,volume,"
        "amount,turnover_rate,pe,pb,market_cap,float_cap,amplitude,high,low,open,prev_close,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, asset_type=excluded.asset_type,"
        "price=excluded.price, pct_change=excluded.pct_change, change=excluded.change,"
        "volume=excluded.volume, amount=excluded.amount, turnover_rate=excluded.turnover_rate,"
        "pe=excluded.pe, pb=excluded.pb, market_cap=excluded.market_cap, float_cap=excluded.float_cap,"
        "amplitude=excluded.amplitude, high=excluded.high, low=excluded.low, open=excluded.open,"
        "prev_close=excluded.prev_close, updated_at=excluded.updated_at",
        snap_rows,
    )
    db.executemany(
        "INSERT INTO instruments(symbol,name,asset_type,market,pinyin,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, asset_type=excluded.asset_type,"
        "market=excluded.market, updated_at=excluded.updated_at",
        inst_rows,
    )
    db.kv_set(f"snapshot_at:{kind}", ts)
    db.kv_set(f"snapshot_stats:{kind}", stats)
    log.info(
        "快照 %s 完成：%d 条，来源 %s", kind, len(snap_rows), stats.get("source", "?")
    )
    return len(snap_rows)


# 后台快照刷新状态
_refresh_lock = threading.Lock()
_refreshing: set[str] = set()
_refresh_state: dict[str, dict[str, Any]] = {}

# 快照整体重试的退避阶梯（秒）。
# 存在的意义：东财的封禁是**按 IP 且会持续一段时间**的，实测连续测试后
# 全部接口都会返回连接被断开。如果失败后前端每次轮询都重新发起抓取，
# 就变成对一个已经封禁的接口做请求放大 —— 越试越封。
# 所以失败次数越多，等得越久。
_BACKOFF_LADDER = (60.0, 180.0, 600.0, 900.0)


def _backoff_remaining(kind: str) -> float:
    """返回距离下次允许重试还有多少秒（0 表示可以重试）。"""
    st = _refresh_state.get(kind) or {}
    if st.get("ok") is not False:
        return 0.0
    fails = int(st.get("consecutive_failures") or 1)
    wait = _BACKOFF_LADDER[min(fails - 1, len(_BACKOFF_LADDER) - 1)]
    finished = st.get("finished_ts")
    if not finished:
        return 0.0
    return max(0.0, wait - (time.time() - finished))


def snapshot_is_fresh(kind: str = "a_share", max_age: int = 86400) -> bool:
    ts = db.kv_get(f"snapshot_at:{kind}")
    if not ts:
        return False
    try:
        return time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < max_age
    except (ValueError, TypeError):
        return False


def _bg_refresh(kind: str, attempts: int = 3) -> None:
    """后台刷新快照，带整体重试。

    为什么要整体重试：实测东财偶发频控会让「首页抓取」直接失败，
    而首屏失败 = 整个刷新失败。若只试一次就放弃，NAS 上首次开机碰到
    一次抖动就会得到空系统 —— 而且要等到下一个定时任务（可能是下个
    交易日 9:05）才会再抓。所以这里做退避重试。
    """
    started = time.time()
    last_err = ""
    try:
        for attempt in range(attempts):
            try:
                n = refresh_snapshot(kind)
                _refresh_state[kind] = {
                    "running": False, "ok": True, "count": n,
                    "attempts": attempt + 1, "consecutive_failures": 0,
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_ts": time.time(),
                }
                log.info("后台快照刷新完成 %s: %d 条（第 %d 次尝试）", kind, n, attempt + 1)
                return
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "后台快照刷新失败 %s（第 %d/%d 次）: %s",
                    kind, attempt + 1, attempts, exc,
                )
                if attempt < attempts - 1:
                    time.sleep(12.0 * (attempt + 1))   # 频控需要时间消退
        prev_fails = int((_refresh_state.get(kind) or {}).get("consecutive_failures") or 0)
        _refresh_state[kind] = {
            "running": False, "ok": False, "error": last_err, "attempts": attempts,
            "consecutive_failures": prev_fails + 1,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_ts": time.time(),
        }
        log.error("后台快照刷新最终失败 %s（已重试 %d 次）: %s", kind, attempts, last_err)
    finally:
        with _refresh_lock:
            _refreshing.discard(kind)
        _refresh_state.setdefault(kind, {})["elapsed"] = round(time.time() - started, 1)


def ensure_snapshot(
    kind: str = "a_share",
    max_age: int = 86400,
    blocking: bool = False,
) -> bool:
    """确保快照可用。

    默认**不阻塞**：数据缺失或过期时，启动一个后台线程去抓，立即返回。
    这一点很关键 —— 群晖上第一次打开首页时数据库是空的，同步抓全市场
    要 1-2 分钟，会把首页卡死（实测浏览器直接超时）。
    需要结果的调用方（比如定时任务）传 blocking=True。

    返回 True 表示快照当前可用。
    """
    if snapshot_is_fresh(kind, max_age):
        return True

    if blocking:
        try:
            refresh_snapshot(kind)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("快照刷新失败 %s: %s", kind, exc)
            return False

    # 退避期内的失败不要立刻重试，避免对已封禁的接口做请求放大
    if _backoff_remaining(kind) > 0:
        log.debug("快照 %s 处于退避期，暂不重试", kind)
        return False

    with _refresh_lock:
        already = kind in _refreshing
        if not already:
            _refreshing.add(kind)
    if not already:
        _refresh_state[kind] = {
            "running": True, "ok": None,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        threading.Thread(
            target=_bg_refresh, args=(kind,), name=f"snapshot-{kind}", daemon=True
        ).start()
    return False


def snapshot_status(auto_retry: bool = True) -> dict[str, Any]:
    """快照就绪状态，供前端轮询提示用，并报告数据完整性。

    auto_retry: 若快照仍为空且当前没在抓，就自动再发起一次后台刷新。
                这使得前端的进度轮询具备自愈能力 —— 否则一次失败后
                轮询会永远停在「正在初始化」，用户只能手动点刷新。
    """
    if auto_retry and not db.kv_get("snapshot_at:a_share") and "a_share" not in _refreshing:
        # 兜底重试；ensure_snapshot 内部会根据失败次数做退避，
        # 所以这里不会变成对已封禁接口的请求放大。
        ensure_snapshot("a_share")

    meta = snapshot_meta()
    count = meta.get("count") or 0
    stats = db.kv_get("snapshot_stats:a_share") or {}
    expected = stats.get("expected_total") or 0
    fetched = stats.get("fetched") or 0
    # a_share 单独计数，避免和「全部标的」总数混在一起看
    a_count = 0
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM market_snapshot WHERE asset_type IN ('stock')"
    )
    if row:
        a_count = row["c"] or 0
    complete = None
    if expected:
        complete = fetched >= expected
    return {
        "ready": count > 0,
        "count": count,              # 全部标的（A股+港股+ETF）
        "a_share_count": a_count,
        "updated_at": meta.get("a_share"),
        "expected": expected,
        "fetched": fetched,
        "complete": complete,
        "failed_pages": stats.get("failed_pages"),
        "source": stats.get("source"),
        "retry_in": round(_backoff_remaining("a_share")),
        "last_error": (_refresh_state.get("a_share") or {}).get("error"),
        "refreshing": sorted(_refreshing),
        "progress": {
            k: {kk: vv for kk, vv in v.items() if kk != "running"}
            for k, v in _refresh_state.items()
        },
    }


def _looks_like_full_code(kw: str) -> bool:
    """判断输入是否是一个「完整」代码，而非待补全的前缀。

    6 位纯数字 -> A股/ETF/指数；带 .SH/.HK 等后缀或 sh/sz/hk 前缀 -> 明确指定。
    1~5 位纯数字（如 "6005"、"700"）一律视为前缀，交给模糊搜索，
    否则 "6005" 会被当成港股 06005.HK，把 A股 6005xx 全部挤掉。
    """
    k = kw.strip().upper().replace(" ", "")
    if re.fullmatch(r"\d{6}", k):
        return True
    if re.fullmatch(r"(SH|SZ|BJ|HK|US)\d{1,6}", k):
        return True
    if "." in k and re.fullmatch(r"[0-9A-Z]{1,6}\.[A-Z]{2}", k):
        return True
    return False


def search(keyword: str, limit: int = 20) -> list[dict]:
    """搜索标的：完整代码精确匹配 > 代码前缀 > 名称包含。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def push(item: dict) -> None:
        sym = item.get("symbol")
        if sym and sym not in seen:
            seen.add(sym)
            item["board"] = item.get("board") or board_of(sym)
            out.append(item)

    # 1) 完整代码 -> 精确匹配，直接返回
    if _looks_like_full_code(kw):
        try:
            sym = normalize(kw)
            quote = get_quotes([sym]).get(sym)
            if quote:
                push({
                    "symbol": sym, "name": quote.get("name") or display_name(sym),
                    "asset_type": asset_type(sym), "board": board_of(sym),
                    "price": quote.get("price"), "pct_change": quote.get("pct_change"),
                })
                return out[:limit]
        except SymbolError:
            pass

    # 2) 名称精确匹配优先，其次代码前缀，最后名称包含。
    #    注意必须转义 LIKE 的通配符：用户搜 "_" 或 "%" 时若不转义，
    #    LIKE 会匹配所有记录，返回一堆毫不相关的股票（实际踩过）。
    esc_kw = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.query(
        "SELECT symbol, name, asset_type, price, pct_change, amount FROM market_snapshot "
        "WHERE symbol LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
        "ORDER BY CASE WHEN name = ? THEN 0 WHEN symbol LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END, "
        "CASE asset_type WHEN 'stock' THEN 0 WHEN 'etf' THEN 1 ELSE 2 END, "
        "amount DESC LIMIT ?",
        (f"{esc_kw}%", f"%{esc_kw}%", kw, f"{esc_kw}%", limit),
    )
    for r in rows:
        d = dict(r)
        d.pop("amount", None)
        push(d)

    if out:
        return out[:limit]

    # 3) 快照还没建时，回退到行情接口直接猜代码（仅限看起来完整的代码）
    if _looks_like_full_code(kw):
        try:
            sym = normalize(kw)
        except SymbolError:
            return out
        q = get_quotes([sym]).get(sym)
        if q:
            push({
                "symbol": sym, "name": q.get("name"), "asset_type": asset_type(sym),
                "board": board_of(sym), "price": q.get("price"),
                "pct_change": q.get("pct_change"),
            })
    return out[:limit]


def resolve_symbol(raw: str) -> str | None:
    """把用户输入解析成标准代码。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        sym = normalize(raw)
        # 指数白名单优先
        if sym in INDEX_CODES:
            return sym
        if get_quotes([sym]).get(sym):
            return sym
    except SymbolError:
        pass
    hits = search(raw, limit=1)
    return hits[0]["symbol"] if hits else None


def get_fundamentals(symbol: str, force: bool = False) -> dict:
    """财务数据，本地缓存 1 天。"""
    sym = normalize(symbol)
    if not force:
        row = db.query_one(
            "SELECT payload, updated_at FROM fundamentals WHERE symbol=? "
            "ORDER BY report_date DESC LIMIT 1",
            (sym,),
        )
        if row:
            ts = row["updated_at"] or ""
            try:
                if time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")) < 86400:
                    import json
                    return json.loads(row["payload"])
            except (ValueError, TypeError):
                pass
    try:
        data = eastmoney.fundamentals(sym)
    except Exception as exc:  # noqa: BLE001
        # 回落本地
        import json
        row = db.query_one(
            "SELECT payload FROM fundamentals WHERE symbol=? ORDER BY report_date DESC LIMIT 1",
            (sym,),
        )
        if row:
            return json.loads(row["payload"])
        raise FetchError(f"财务数据获取失败 {sym}: {exc}") from exc

    import json
    rd = (data.get("latest") or {}).get("report_date") or "unknown"
    db.execute(
        "INSERT INTO fundamentals(symbol,report_date,payload,updated_at) VALUES(?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(symbol,report_date) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        (sym, rd, json.dumps(data, ensure_ascii=False)),
    )
    return data


def snapshot_rows(
    where: str = "",
    params: tuple = (),
    order: str = "amount DESC",
    limit: int = 100,
) -> list[dict]:
    sql = f"SELECT * FROM market_snapshot {'WHERE ' + where if where else ''} ORDER BY {order} LIMIT ?"
    return db.rows_to_dicts(db.query(sql, (*params, limit)))


def snapshot_meta() -> dict[str, Any]:
    return {
        "a_share": db.kv_get("snapshot_at:a_share"),
        "hk": db.kv_get("snapshot_at:hk"),
        "etf": db.kv_get("snapshot_at:etf"),
        "count": (db.query_one("SELECT COUNT(*) AS c FROM market_snapshot") or {"c": 0})["c"],
    }
