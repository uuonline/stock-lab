"""定时任务调度（APScheduler）。

任务：
  * 全市场快照刷新    —— 开盘日 9:00 / 12:30 / 15:05
  * 盘后 K线同步      —— 交易日 15:30、18:00（默认，可配）
  * 盘中提醒巡检      —— 交易时段每 N 秒
  * 自选股快照存档    —— 每日 15:10
  * 清理过期缓存      —— 每日 03:00
"""
from __future__ import annotations

import logging
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .. import db
from ..config import settings
from ..sources import market
from ..services import alerts, quote

log = logging.getLogger("stocklab.scheduler")

_scheduler: BackgroundScheduler | None = None


def _job_snapshot(kind: str = "a_share") -> None:
    start = time.time()
    try:
        n = market.refresh_snapshot(kind)
        db.log_task(f"snapshot:{kind}", "ok", f"{n} 条", time.time() - start)
        log.info("快照刷新完成 %s: %d 条", kind, n)
    except Exception as exc:  # noqa: BLE001
        db.log_task(f"snapshot:{kind}", "error", str(exc), time.time() - start)
        log.warning("快照刷新失败 %s: %s", kind, exc)


def _job_snapshot_all() -> None:
    for kind in ("a_share", "hk", "etf"):
        _job_snapshot(kind)
        time.sleep(1.0)


def _job_sync_watchlist_kline() -> None:
    """盘后把自选股日线补齐到本地库。"""
    start = time.time()
    rows = db.query("SELECT DISTINCT symbol FROM watchlist")
    ok = fail = 0
    for r in rows:
        sym = r["symbol"]
        try:
            bars = market.get_kline(sym, "day", 300, use_local=False)
            if bars:
                db.save_kline(sym, bars)
                ok += 1
            else:
                fail += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            log.debug("同步K线失败 %s: %s", sym, exc)
        time.sleep(0.25)
    db.log_task("sync_watchlist", "ok" if not fail else "partial",
                f"成功 {ok} 失败 {fail}", time.time() - start)
    log.info("自选股K线同步完成: 成功 %d, 失败 %d", ok, fail)


def _job_check_alerts() -> None:
    if not _is_trading_time():
        return
    start = time.time()
    try:
        fired = alerts.check_all_quotes()
        if fired:
            db.log_task("alerts", "ok", f"触发 {len(fired)} 条", time.time() - start)
    except Exception as exc:  # noqa: BLE001
        db.log_task("alerts", "error", str(exc), time.time() - start)
        log.warning("提醒巡检失败: %s", exc)


def _is_trading_time() -> bool:
    st = quote.market_status()
    return bool(st.get("a_share_open") or st.get("hk_open"))


def _job_cleanup() -> None:
    start = time.time()
    try:
        from ..sources.base import cache
        cache.clear()
        db.execute("DELETE FROM kline_daily WHERE trade_date < date('now','-5 years')")
        db.log_task("cleanup", "ok", "缓存与历史数据清理完成", time.time() - start)
    except Exception as exc:  # noqa: BLE001
        db.log_task("cleanup", "error", str(exc), time.time() - start)


def start() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled:
        log.info("调度器已禁用 (SL_SCHEDULER_ENABLED=0)")
        return None
    if _scheduler is not None:
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler as BS
        sched = BS(timezone=settings.timezone, daemon=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("调度器初始化失败（时区数据缺失？）: %s", exc)
        sched = BackgroundScheduler(daemon=True)

    # 全市场快照
    sched.add_job(_job_snapshot_all, CronTrigger(day_of_week="mon-fri", hour=9, minute=5),
                  id="snapshot_morning", replace_existing=True)
    sched.add_job(_job_snapshot_all, CronTrigger(day_of_week="mon-fri", hour=12, minute=35),
                  id="snapshot_noon", replace_existing=True)
    sched.add_job(_job_snapshot_all, CronTrigger(day_of_week="mon-fri", hour=15, minute=5),
                  id="snapshot_close", replace_existing=True)

    # 盘后 K线同步
    for i, t in enumerate(settings.eod_fetch_times):
        try:
            hh, mm = t.split(":")
            sched.add_job(_job_sync_watchlist_kline,
                          CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm)),
                          id=f"sync_kline_{i}", replace_existing=True)
        except (ValueError, IndexError):
            log.warning("无法解析盘后抓取时间: %s", t)

    # 盘中提醒巡检
    if settings.intrady_check_interval > 0:
        sched.add_job(_job_check_alerts,
                      IntervalTrigger(seconds=settings.intrady_check_interval),
                      id="check_alerts", replace_existing=True,
                      max_instances=1, coalesce=True)

    # 清理
    sched.add_job(_job_cleanup, CronTrigger(hour=3, minute=0),
                  id="cleanup", replace_existing=True)

    sched.start()
    _scheduler = sched
    log.info("调度器已启动，共 %d 个任务", len(sched.get_jobs()))
    return sched


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None


def jobs() -> list[dict]:
    if _scheduler is None:
        return []
    out = []
    for j in _scheduler.get_jobs():
        nxt = getattr(j, "next_run_time", None)
        out.append({
            "id": j.id,
            "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
            "trigger": str(j.trigger),
        })
    return out


def recent_logs(limit: int = 50) -> list[dict]:
    return db.rows_to_dicts(
        db.query("SELECT * FROM task_logs ORDER BY id DESC LIMIT ?", (limit,))
    )


def run_now(task: str) -> str:
    """手动触发任务。"""
    mapping = {
        "snapshot": _job_snapshot_all,
        "sync_kline": _job_sync_watchlist_kline,
        "alerts": alerts.check_all_quotes,
        "cleanup": _job_cleanup,
    }
    fn = mapping.get(task)
    if fn is None:
        raise ValueError(f"未知任务: {task}")
    fn()
    return f"任务 {task} 已执行"
