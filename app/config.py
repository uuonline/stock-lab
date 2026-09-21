"""全局配置：全部通过环境变量覆盖，方便在群晖 Docker 里改 .env 即可。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    v = _env(key).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # ---- 基础 ----
    app_name: str = "StockLab"
    host: str = field(default_factory=lambda: _env("SL_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("SL_PORT", 8787))
    timezone: str = field(default_factory=lambda: _env("SL_TZ", "Asia/Shanghai"))
    # 局域网内可设为空以关闭鉴权；公网暴露务必设置
    access_token: str = field(default_factory=lambda: _env("SL_ACCESS_TOKEN", ""))
    debug: bool = field(default_factory=lambda: _env_bool("SL_DEBUG", False))

    # ---- 路径 ----
    data_dir: Path = field(
        default_factory=lambda: Path(_env("SL_DATA_DIR", str(BASE_DIR / "data")))
    )

    # ---- 数据源行为 ----
    http_timeout: float = field(default_factory=lambda: _env_float("SL_HTTP_TIMEOUT", 12.0))
    http_retries: int = field(default_factory=lambda: _env_int("SL_HTTP_RETRIES", 4))
    http_backoff: float = field(default_factory=lambda: _env_float("SL_HTTP_BACKOFF", 0.6))
    # 东财有频控，串行请求之间留最小间隔
    min_request_interval: float = field(
        default_factory=lambda: _env_float("SL_MIN_REQUEST_INTERVAL", 0.18)
    )
    quote_cache_ttl: int = field(default_factory=lambda: _env_int("SL_QUOTE_CACHE_TTL", 5))
    kline_cache_ttl: int = field(default_factory=lambda: _env_int("SL_KLINE_CACHE_TTL", 900))
    snapshot_cache_ttl: int = field(default_factory=lambda: _env_int("SL_SNAPSHOT_CACHE_TTL", 60))
    source_order: list[str] = field(
        default_factory=lambda: [
            s.strip() for s in _env(
                "SL_SOURCE_ORDER", "tencent,eastmoney,sina,alphavantage"
            ).split(",") if s.strip()
        ]
    )
    # 字段补充源：主源拿不到的字段（如主力净流入）由这些源补，best-effort
    enrich_sources: list[str] = field(
        default_factory=lambda: [
            s.strip() for s in _env("SL_ENRICH_SOURCES", "eastmoney").split(",") if s.strip()
        ]
    )
    # 补充请求的快速模式重试次数（比主请求少，避免拖慢响应）
    enrich_retries: int = field(default_factory=lambda: _env_int("SL_ENRICH_RETRIES", 1))
    # 单个数据源取 K线的墙钟预算（秒）。
    # 实测东财 K线整族挂掉时要 13 秒才失败，而腾讯只需 250ms ——
    # 没有这道闸门，首次加载 K线就会白等十几秒。
    kline_source_budget: float = field(
        default_factory=lambda: _env_float("SL_KLINE_SOURCE_BUDGET", 2.5)
    )
    # 字段补充（主力净流入）的时间预算（秒）。
    # 健康时东财约 250ms 就能返回，0.9s 足够；
    # 数据源抽风时最多让详情页多等 0.9 秒而不是几秒。
    # 连续失败后该源会被熔断，后续请求直接跳过，不再付出这个代价。
    enrich_budget: float = field(default_factory=lambda: _env_float("SL_ENRICH_BUDGET", 0.9))

    # ---- 回测默认参数 ----
    commission_rate: float = field(default_factory=lambda: _env_float("SL_COMMISSION", 0.00025))
    stamp_tax_rate: float = field(default_factory=lambda: _env_float("SL_STAMP_TAX", 0.0005))
    slippage_rate: float = field(default_factory=lambda: _env_float("SL_SLIPPAGE", 0.0005))
    initial_cash: float = field(default_factory=lambda: _env_float("SL_INITIAL_CASH", 100000.0))

    # ---- Alpha Vantage（可选，A股/美股的备用源）----
    # 实测支持：美股行情/日线/财务，A股行情/日线；不支持港股与A股财务。
    # ⚠️ 一次请求只能查一只标的，额度很紧，所以默认不放在源顺序前列。
    av_api_key: str = field(default_factory=lambda: _env("SL_ALPHAVANTAGE_KEY", ""))
    av_daily_limit: int = field(
        default_factory=lambda: _env_int("SL_ALPHAVANTAGE_DAILY_LIMIT", 25)
    )
    av_quote_cache_ttl: int = field(
        default_factory=lambda: _env_int("SL_ALPHAVANTAGE_QUOTE_TTL", 300)
    )
    av_kline_cache_ttl: int = field(
        default_factory=lambda: _env_int("SL_ALPHAVANTAGE_KLINE_TTL", 21600)
    )
    # 官方免费额度：25 次/天 + 每秒 1 次。实测连发第 2 次就被挡，
    # 所以两次请求之间强制留间隔（默认 1.2 秒，略高于官方 1 秒）
    av_min_interval: float = field(
        default_factory=lambda: _env_float("SL_ALPHAVANTAGE_MIN_INTERVAL", 1.2)
    )
    # 仍被每秒限流时的退避基数（秒），实际等待 = 基数 × 第几次重试
    av_burst_wait: float = field(
        default_factory=lambda: _env_float("SL_ALPHAVANTAGE_BURST_WAIT", 3.0)
    )

    # ---- AI ----
    ai_enabled: bool = field(default_factory=lambda: _env_bool("SL_AI_ENABLED", False))
    ai_base_url: str = field(
        default_factory=lambda: _env("SL_AI_BASE_URL", "https://api.deepseek.com/v1")
    )
    ai_api_key: str = field(default_factory=lambda: _env("SL_AI_API_KEY", ""))
    ai_model: str = field(default_factory=lambda: _env("SL_AI_MODEL", "deepseek-chat"))
    ai_timeout: float = field(default_factory=lambda: _env_float("SL_AI_TIMEOUT", 120.0))

    # ---- 通知渠道（留空即禁用）----
    notify_wecom_webhook: str = field(default_factory=lambda: _env("SL_NOTIFY_WECOM", ""))
    notify_telegram_token: str = field(default_factory=lambda: _env("SL_NOTIFY_TG_TOKEN", ""))
    notify_telegram_chat: str = field(default_factory=lambda: _env("SL_NOTIFY_TG_CHAT", ""))
    notify_serverchan_key: str = field(default_factory=lambda: _env("SL_NOTIFY_SERVERCHAN", ""))
    notify_bark_url: str = field(default_factory=lambda: _env("SL_NOTIFY_BARK", ""))
    notify_generic_webhook: str = field(default_factory=lambda: _env("SL_NOTIFY_WEBHOOK", ""))

    # ---- 调度 ----
    scheduler_enabled: bool = field(default_factory=lambda: _env_bool("SL_SCHEDULER_ENABLED", True))
    # 盘后自动抓取时间（24 小时制，逗号分隔）
    eod_fetch_times: list[str] = field(
        default_factory=lambda: [t.strip() for t in _env("SL_EOD_TIMES", "15:30,18:00").split(",") if t.strip()]
    )
    # 盘中异动巡检间隔（秒），0 关闭
    intrady_check_interval: int = field(
        default_factory=lambda: _env_int("SL_INTRADAY_INTERVAL", 60)
    )

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        for sub in ("cache", "db", "reports", "logs"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "stocklab.sqlite3"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def web_dir(self) -> Path:
        return BASE_DIR / "app" / "web"


settings = Settings()
