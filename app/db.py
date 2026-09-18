"""SQLite 数据层。

刻意不引入 ORM：NAS 上部署越少依赖越稳，且本项目的查询都是简单 KV / 时间序列。
所有写入走 WAL 模式，避免 Web 读取时被定时任务写阻塞。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from .config import settings

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- 自选股 / 分组
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name  TEXT    NOT NULL DEFAULT '默认分组',
    symbol      TEXT    NOT NULL,           -- 600519.SH
    name        TEXT    NOT NULL DEFAULT '',
    asset_type  TEXT    NOT NULL DEFAULT 'stock',  -- stock/etf/index/hk
    note        TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(group_name, symbol)
);

-- 日线（本地沉淀，回测与指标都读这里）
CREATE TABLE IF NOT EXISTS kline_daily (
    symbol   TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open     REAL, high REAL, low REAL, close REAL,
    volume   REAL, amount REAL,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_kline_symbol ON kline_daily(symbol, trade_date DESC);

-- 基本面快照
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol      TEXT NOT NULL,
    report_date TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (symbol, report_date)
);

-- 全市场快照（选股用）
CREATE TABLE IF NOT EXISTS market_snapshot (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    asset_type  TEXT,
    price       REAL, pct_change REAL, change REAL,
    volume      REAL, amount REAL, turnover_rate REAL,
    pe REAL, pb REAL, market_cap REAL, float_cap REAL,
    amplitude   REAL, high REAL, low REAL, open REAL, prev_close REAL,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_pct ON market_snapshot(pct_change DESC);
CREATE INDEX IF NOT EXISTS idx_snap_amount ON market_snapshot(amount DESC);

-- 股票基础信息（代码 -> 名称/类型）
CREATE TABLE IF NOT EXISTS instruments (
    symbol     TEXT PRIMARY KEY,
    name       TEXT,
    asset_type TEXT,
    market     TEXT,
    pinyin     TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inst_name ON instruments(name);

-- 提醒规则
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    rule_type   TEXT NOT NULL,   -- price_above/price_below/pct_up/pct_down/indicator
    params      TEXT NOT NULL DEFAULT '{}',  -- JSON
    message     TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    cooldown    INTEGER NOT NULL DEFAULT 1800,  -- 秒
    last_fired  REAL NOT NULL DEFAULT 0,
    fired_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 提醒触发历史
CREATE TABLE IF NOT EXISTS alert_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id   INTEGER,
    symbol     TEXT, name TEXT, rule_type TEXT,
    message    TEXT, price REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 回测记录
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    start_date  TEXT, end_date TEXT,
    metrics     TEXT NOT NULL DEFAULT '{}',
    equity      TEXT NOT NULL DEFAULT '[]',
    trades      TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 筛选器保存的方案
CREATE TABLE IF NOT EXISTS screens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- AI 报告
CREATE TABLE IF NOT EXISTS ai_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    name       TEXT,
    kind       TEXT NOT NULL DEFAULT 'single',
    content    TEXT NOT NULL,
    model      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 任务执行日志
CREATE TABLE IF NOT EXISTS task_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task       TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    duration   REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 通用 KV（存调度状态、上次同步时间等）
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(settings.db_path), timeout=30.0, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, params).fetchall())


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def executemany(sql: str, rows: Iterable[Sequence[Any]]) -> int:
    with tx() as conn:
        cur = conn.executemany(sql, rows)
        return cur.rowcount


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------- KV helpers ----------------

def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(k,v,updated_at) VALUES(?,?,datetime('now','localtime')) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def kv_get(key: str, default: Any = None) -> Any:
    row = query_one("SELECT v FROM kv WHERE k=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except (json.JSONDecodeError, TypeError):
        return default


def log_task(task: str, status: str, detail: str = "", duration: float = 0.0) -> None:
    try:
        execute(
            "INSERT INTO task_logs(task,status,detail,duration) VALUES(?,?,?,?)",
            (task, status, detail[:2000], round(duration, 3)),
        )
        # 只保留最近 500 条
        execute(
            "DELETE FROM task_logs WHERE id NOT IN "
            "(SELECT id FROM task_logs ORDER BY id DESC LIMIT 500)"
        )
    except sqlite3.Error:
        pass


def save_kline(symbol: str, bars: list[dict]) -> int:
    """bars: [{date, open, high, low, close, volume, amount}]"""
    if not bars:
        return 0
    rows = [
        (
            symbol,
            b["date"],
            b.get("open"), b.get("high"), b.get("low"), b.get("close"),
            b.get("volume"), b.get("amount"),
        )
        for b in bars
        if b.get("date")
    ]
    return executemany(
        "INSERT INTO kline_daily(symbol,trade_date,open,high,low,close,volume,amount) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,trade_date) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume, amount=excluded.amount",
        rows,
    )


def load_kline(symbol: str, start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT trade_date AS date, open, high, low, close, volume, amount FROM kline_daily WHERE symbol=?"
    params: list[Any] = [symbol]
    if start:
        sql += " AND trade_date>=?"
        params.append(start)
    if end:
        sql += " AND trade_date<=?"
        params.append(end)
    sql += " ORDER BY trade_date ASC"
    return rows_to_dicts(query(sql, params))


def kline_range(symbol: str) -> tuple[str | None, str | None]:
    row = query_one(
        "SELECT MIN(trade_date) AS a, MAX(trade_date) AS b FROM kline_daily WHERE symbol=?",
        (symbol,),
    )
    if not row:
        return None, None
    return row["a"], row["b"]


def now_ts() -> float:
    return time.time()
