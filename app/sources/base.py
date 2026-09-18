"""HTTP 抓取基座：限速 + 退避重试 + 磁盘缓存。

东财接口实测存在频控：并发或高频请求会返回空 body（curl 52），
几百毫秒后自动恢复。因此这里做三件事：
  1. 全局最小请求间隔（串行化，避免打爆）
  2. 空响应 / 5xx / 超时 一律视为可重试，指数退避 + 抖动
  3. 进程内 TTL 缓存，热点数据（行情、K线）不打重复请求
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("stocklab.http")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


class RateLimiter:
    """全局串行限速器，保证任意两次外网请求之间至少间隔 min_interval 秒。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class TTLCache:
    def __init__(self, maxsize: int = 4096) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expire_at, value = item
            if expire_at < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self._maxsize:
                now = time.monotonic()
                for k in [k for k, (exp, _) in self._data.items() if exp < now]:
                    self._data.pop(k, None)
                if len(self._data) >= self._maxsize:
                    for k in list(self._data)[: self._maxsize // 4]:
                        self._data.pop(k, None)
            self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


cache = TTLCache()
_limiter = RateLimiter(settings.min_request_interval)

_client_lock = threading.Lock()
_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                headers=DEFAULT_HEADERS,
                timeout=httpx.Timeout(settings.http_timeout, connect=8.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                trust_env=False,  # 忽略系统代理，NAS 上更可控
            )
        return _client


class FetchError(RuntimeError):
    pass


def fetch_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    encoding: str | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    use_cache: bool = True,
    cache_ttl: float = 0.0,
) -> str:
    key = f"T|{url}|{json.dumps(params, sort_keys=True) if params else ''}"
    if use_cache and cache_ttl > 0:
        hit = cache.get(key)
        if hit is not None:
            return hit

    attempts = retries if retries is not None else settings.http_retries
    last_err: Exception | None = None

    for attempt in range(attempts):
        _limiter.wait()
        try:
            resp = client().get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code >= 500:
                raise FetchError(f"HTTP {resp.status_code}")
            body = resp.content
            # 东财频控特征：200 但 body 为空
            if not body:
                raise FetchError("empty body (疑似频控)")
            if encoding:
                text = body.decode(encoding, errors="replace")
            else:
                text = resp.text
            if use_cache and cache_ttl > 0:
                cache.set(key, text, cache_ttl)
            return text
        except Exception as exc:  # noqa: BLE001 - 统一退避重试
            last_err = exc
            if attempt < attempts - 1:
                backoff = settings.http_backoff * (2 ** attempt) + random.uniform(0, 0.35)
                time.sleep(min(backoff, 6.0))

    raise FetchError(f"请求失败 {url}: {last_err}")


def fetch_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    use_cache: bool = True,
    cache_ttl: float = 0.0,
) -> Any:
    text = fetch_text(
        url,
        params=params,
        headers=headers,
        retries=retries,
        timeout=timeout,
        use_cache=use_cache,
        cache_ttl=cache_ttl,
    )
    text = text.strip()
    # 部分接口返回 JSONP: cb({...})
    if text and not text.startswith(("{", "[")):
        start = text.find("(")
        end = text.rfind(")")
        if start != -1 and end > start:
            text = text[start + 1 : end]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"JSON 解析失败: {text[:200]}") from exc


def to_float(value: Any) -> float | None:
    """东财用 '-' 表示缺失。"""
    if value is None or value == "" or value == "-":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


# ---------------- 静默失败防护 ----------------
#
# 背景：本项目出过一次严重事故 —— 给函数加了参数却忘了改被调函数签名，
# 导致数据源每次调用都抛 TypeError，而兜底逻辑把它当成"网络失败"吞掉、
# 改用备用源。表面上一切正常，实际上那条通道 100% 是死的。
#
# 教训：兜底可以吞网络错误，**绝不能吞编程错误**。
# 下面这个判别器用于所有 catch-all 的位置。

PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, ImportError, SyntaxError)


def is_programming_error(exc: BaseException) -> bool:
    return isinstance(exc, PROGRAMMING_ERRORS)


def swallow(
    exc: BaseException,
    logger: logging.Logger,
    context: str,
    *,
    expected: tuple[type[BaseException], ...] = (),
    level: int = logging.DEBUG,
) -> None:
    """按异常类型决定记录级别。

    编程错误 -> ERROR（带堆栈），提醒必须修复；
    预期内错误 -> DEBUG/WARNING，不刷屏。
    """
    if is_programming_error(exc):
        logger.error(
            "%s 存在代码错误（已被跳过，请修复）: %s", context, exc, exc_info=True
        )
    elif expected and isinstance(exc, expected):
        logger.log(level, "%s: %s", context, exc)
    else:
        logger.warning("%s 异常: %s", context, exc)


# ---------------- 分片主机池 ----------------
#
# 实测结论：东财主域名 push2.eastmoney.com 在连续请求后会返回
# "Server disconnected" / 空 body，而编号分片（1.push2 / 92.push2 / ...）
# 仍然可用。反过来说主域名稍后也会恢复。
# 因此不能把主机写死 —— 维护一个主机池，失败即冷却该主机并轮换下一个，
# 全部冷却时清空冷却重试。这是保证 NAS 上 7x24 稳定抓取的关键。

class CircuitBreaker:
    """按数据源熔断。

    实测发现：东财的限流是**按 IP** 的，不是按域名 —— 被打限流时 12 个
    分片主机**全部**同时失败。这种情况下主机轮换毫无用处，只会让每个请求
    都白白等满重试时间（实测每次 3 秒）。

    熔断器解决的就是这个：连续失败若干次后，直接跳过该源进入冷却，
    请求立刻失败并转入下一个源，而不是每次都硬等。
    """

    def __init__(self, name: str, threshold: int = 5, cooldown: float = 45.0) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._fail = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._fail = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._fail += 1
            if self._fail >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown
                self._fail = 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            left = self._open_until - time.monotonic()
            return {
                "name": self.name,
                "open": left > 0,
                "remaining": round(left, 1) if left > 0 else 0.0,
                "consecutive_failures": self._fail,
            }


# 每个数据源的熔断器
BREAKERS: dict[str, CircuitBreaker] = {
    "eastmoney": CircuitBreaker("eastmoney"),
    "tencent": CircuitBreaker("tencent"),
    "sina": CircuitBreaker("sina"),
}


def breakers_status() -> dict[str, Any]:
    return {k: v.status() for k, v in BREAKERS.items()}


class HostPool:
    def __init__(self, hosts: list[str], cooldown: float = 150.0, max_wait: float = 0.6) -> None:
        self.hosts = list(hosts)
        self.cooldown = cooldown
        # 全部主机冷却时最多等待多久再试。设小一点：IP 级限流时等待没有意义，
        # 快速失败并让熔断器接管，好过每个请求都卡 3 秒。
        self.max_wait = max_wait
        self._bad: dict[str, float] = {}
        self._idx = 0
        self._lock = threading.Lock()

    def candidates(self) -> list[str]:
        """返回按可用性排序的主机列表（轮转起点，避免总打同一台）。

        关键：当所有主机都在冷却中时，**不能**清空冷却并把全部主机再打一遍 ——
        那样一次失败会引发 12 台 × 重试次数的请求风暴，把频控推向更糟。
        正确做法是等到最早恢复的那台，只试它一台。
        """
        now = time.monotonic()
        with self._lock:
            good = [h for h in self.hosts if self._bad.get(h, 0.0) <= now]
            if good:
                start = self._idx % len(good)
                self._idx += 1
                return good[start:] + good[:start]

            if not self._bad:
                return []

            # 全部冷却：只挑最早恢复的一台，必要时短暂等待
            host, until = min(self._bad.items(), key=lambda kv: kv[1])
            self._bad.pop(host, None)          # 摘出冷却，交给调用方试用
            self._idx += 1
            wait = min(max(until - now, 0.0), self.max_wait)

        if wait > 0:
            time.sleep(wait)                    # 锁外等待，不阻塞其他线程
        return [host]

    def mark_bad(self, host: str) -> None:
        with self._lock:
            self._bad[host] = time.monotonic() + self.cooldown

    def mark_good(self, host: str) -> None:
        with self._lock:
            self._bad.pop(host, None)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "hosts": self.hosts,
            "cooldown": {h: round(t - now, 1) for h, t in self._bad.items() if t > now},
        }


def _swap_host(url: str, host: str) -> str:
    """把 URL 中的主机名替换为分片主机。"""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, path = rest.partition("/")
    return f"{scheme}://{host}/{path}"


def fetch_rotating(
    url: str,
    pool: HostPool,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    encoding: str | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    use_cache: bool = True,
    cache_ttl: float = 0.0,
) -> str:
    """在主机池上轮换抓取，任一主机成功即返回。

    retries: 单主机内的重试次数。注意这是**每个主机**的次数，
             默认取 2（比直连模式的 http_retries 少），
             因为失败后换主机通常比原地重试更快恢复。
    """
    candidates = pool.candidates()
    if not candidates:
        raise FetchError(f"主机池为空: {url}")

    per_host = 2 if retries is None else max(1, retries)
    last_err: Exception | None = None
    for host in candidates:
        target = _swap_host(url, host)
        try:
            text = fetch_text(
                target, params=params, headers=headers, encoding=encoding,
                retries=per_host, timeout=timeout,
                use_cache=use_cache, cache_ttl=cache_ttl,
            )
            pool.mark_good(host)
            return text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            pool.mark_bad(host)
            continue
    raise FetchError(f"主机池全部失败 {url}: {last_err}")


def fetch_json_rotating(
    url: str,
    pool: HostPool,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    use_cache: bool = True,
    cache_ttl: float = 0.0,
) -> Any:
    text = fetch_rotating(
        url, pool, params=params, headers=headers,
        retries=retries, timeout=timeout,
        use_cache=use_cache, cache_ttl=cache_ttl,
    )
    text = text.strip()
    if text and not text.startswith(("{", "[")):
        start = text.find("(")
        end = text.rfind(")")
        if start != -1 and end > start:
            text = text[start + 1 : end]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"JSON 解析失败: {text[:200]}") from exc
