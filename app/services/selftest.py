"""部署自检。

存在的意义：Mac 上能跑通不代表 NAS 上能跑通 —— NAS 的 DNS、出口网络、
防火墙策略都可能不同，最常见的就是数据源在 NAS 上被限流或不可达。

因此提供一个可同时通过 Web 接口和命令行调用的自检，覆盖：
数据源连通性、各市场行情、K线、财务、数据库读写、指标正确性、
回测引擎、调度器、磁盘可写性。

命令行用法（部署后第一时间执行）：
    docker exec -it stocklab python -m app.services.selftest
"""
from __future__ import annotations

import math
import os
import platform
import sys
import time
from typing import Any, Callable

from .. import db
from ..config import settings
from ..sources import eastmoney, market, sina, tencent
from ..sources.base import FetchError
from ..symbols import normalize


class SkipCheck(Exception):
    """前置条件不满足（比如快照还没建），跳过该检查 —— 不算失败。

    典型场景：NAS 全新安装时先跑自检，此时全市场快照本来就还没抓，
    报成「核心失败」会误导用户以为系统坏了。
    """


class Check:
    __slots__ = ("name", "ok", "detail", "ms", "level", "group", "skipped")

    def __init__(self, name: str, ok: bool, detail: str = "", ms: float = 0.0,
                 level: str = "core", skipped: bool = False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.ms = ms
        self.level = level   # core = 必须通过；optional = 失败可接受
        self.group = ""
        self.skipped = skipped

    def to_dict(self) -> dict:
        return {
            "name": self.name, "ok": self.ok, "detail": self.detail,
            "ms": round(self.ms, 1), "level": self.level, "group": self.group,
            "skipped": self.skipped,
        }


def _run(name: str, fn: Callable[[], str], level: str = "core") -> Check:
    t0 = time.perf_counter()
    try:
        detail = fn() or "ok"
        return Check(name, True, detail, (time.perf_counter() - t0) * 1000, level)
    except SkipCheck as exc:
        return Check(name, True, f"跳过（{exc}）",
                     (time.perf_counter() - t0) * 1000, level, skipped=True)
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"{type(exc).__name__}: {exc}",
                     (time.perf_counter() - t0) * 1000, level)


# ---------------- 各项检查 ----------------

def _env_info() -> dict:
    try:
        import numpy
        npv = numpy.__version__
    except ImportError:
        npv = "缺失"
    try:
        import fastapi
        fv = fastapi.__version__
    except ImportError:
        fv = "缺失"
    try:
        import httpx
        hv = httpx.__version__
    except ImportError:
        hv = "缺失"
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "numpy": npv,
        "fastapi": fv,
        "httpx": hv,
        "timezone": time.strftime("%Z"),
        "data_dir": str(settings.data_dir),
        "db_path": str(settings.db_path),
        "source_order": settings.source_order,
    }


def check_data_dir() -> str:
    probe = settings.data_dir / ".write_probe"
    probe.write_text("ok", encoding="utf-8")
    content = probe.read_text(encoding="utf-8")
    probe.unlink()
    if content != "ok":
        raise RuntimeError("写入内容不一致")
    return f"可写: {settings.data_dir}"


def check_db() -> str:
    db.init_db()
    db.kv_set("__selftest__", {"t": time.time()})
    v = db.kv_get("__selftest__")
    if not v:
        raise RuntimeError("KV 读写失败")
    n = db.query_one("SELECT COUNT(*) AS c FROM market_snapshot")
    k = db.query_one("SELECT COUNT(*) AS c FROM kline_daily")
    return f"快照 {n['c']} 条 / K线 {k['c']} 根"


def check_quote_capability() -> str:
    """核心能力检查：不管用哪个源，能拿到行情才算数。"""
    qs = market.get_quotes(["600519.SH", "000001.SZ", "00700.HK"])
    missing = [s for s in ("600519.SH", "000001.SZ", "00700.HK")
               if not qs.get(s) or qs[s].get("price") is None]
    if missing:
        raise RuntimeError(f"以下标的拿不到行情: {missing}")
    src = {q.get("_source") for q in qs.values()}
    return f"3 个标的全部拿到行情（实际使用源: {', '.join(sorted(s for s in src if s))}）"


def check_source_reliability(samples: int = 3) -> str:
    """逐源采样成功率。

    实测本机数据：腾讯/新浪 ~100%，东财 push2 仅 ~42%（间歇性
    RemoteProtocolError）。东财字段最全但可用性差，这正是默认把腾讯
    放在首位、东财仅用于按需补充字段的原因。这里只报告，不判失败。
    """
    # 东财两条探针必须限时：它当前被限流，每轮要 10 秒以上逐台主机重试，
    # 3 轮采样就能把整个自检拖到一分钟。限时后仍能如实反映「失败」。
    probes = [
        ("腾讯·行情", lambda: tencent.quotes(["600519.SH"])),
        ("新浪·行情", lambda: sina.quotes(["600519.SH"])),
        ("东财·行情",
         lambda: eastmoney.quotes(["600519.SH"], deadline=time.monotonic() + 3.0)),
        ("东财·列表",
         lambda: eastmoney.market_list("a_share", 1, 20, deadline=time.monotonic() + 3.0)),
    ]
    parts = []
    for name, fn in probes:
        ok = 0
        for _ in range(samples):
            try:
                r = fn()
                if r and (not isinstance(r, tuple) or r[0]):
                    ok += 1
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        parts.append(f"{name} {ok}/{samples}")
    return " | ".join(parts)


def check_eastmoney_quote() -> str:
    q = eastmoney.quotes(["600519.SH"])
    item = q.get("600519.SH")
    if not item or item.get("price") is None:
        raise RuntimeError("未返回有效行情")
    return f"贵州茅台 {item['price']} ({item.get('pct_change')}%)"


def check_eastmoney_kline() -> str:
    # 东财 K线整族当前被限流，逐台主机重试要 13 秒。这只是可选检查，
    # 不该让整个自检卡在这里 —— 给 4 秒死线，失败就如实报告。
    bars = eastmoney.kline(
        "600519.SH", "day", 30, deadline=time.monotonic() + 4.0
    )
    if len(bars) < 20:
        raise RuntimeError(f"仅返回 {len(bars)} 根K线")
    if bars[-1].get("close") is None:
        raise RuntimeError("收盘价为空")
    return f"{len(bars)} 根，末根 {bars[-1]['date']} 收 {bars[-1]['close']}"


def check_eastmoney_list() -> str:
    # 同 K线：可选检查，限时 4 秒，避免被限流的源拖住整个自检
    rows, total = eastmoney.market_list(
        "a_share", page=1, size=100, deadline=time.monotonic() + 4.0
    )
    if not rows:
        raise RuntimeError("列表为空")
    return f"首页 {len(rows)} 条 / 全市场 {total} 只"


def check_tencent_quote() -> str:
    q = tencent.quotes(["000001.SZ", "00700.HK"])
    if not q:
        raise RuntimeError("未返回任何行情")
    parts = [f"{v.get('name')} {v.get('price')}" for v in list(q.values())[:2]]
    return "; ".join(parts)


def check_tencent_kline() -> str:
    bars = tencent.kline("00700.HK", "day", 30)
    if len(bars) < 10:
        raise RuntimeError(f"仅返回 {len(bars)} 根")
    return f"港股 {len(bars)} 根，末根 {bars[-1]['date']}"


def check_sina_quote() -> str:
    q = sina.quotes(["600519.SH"])
    item = q.get("600519.SH")
    if not item or item.get("price") is None:
        raise RuntimeError("未返回有效行情")
    return f"贵州茅台 {item['price']}"


def check_markets() -> str:
    """四类标的逐一验证，NAS 上最容易出问题的是港股/指数。"""
    syms = ["600519.SH", "000001.SZ", "00700.HK", "510300.SH", "000001.SH", "300750.SZ"]
    qs = market.get_quotes(syms)
    missing = [s for s in syms if not qs.get(s) or qs[s].get("price") is None]
    if missing:
        raise RuntimeError(f"以下标的无行情: {missing}")
    return f"{len(syms)} 个标的全部返回行情"


def check_kline_all_markets() -> str:
    result = []
    for sym in ("600519.SH", "00700.HK", "510300.SH", "000001.SH"):
        bars = market.get_kline(sym, "day", 30)
        if not bars:
            raise RuntimeError(f"{sym} 无K线")
        result.append(f"{sym}:{len(bars)}")
    return " ".join(result)


def check_market_list_capability() -> str:
    """核心能力：不管走东财还是新浪，能拿到全市场列表才算数。

    东财的列表接口会整站封 IP（实测所有分片同时断开），此时新浪兜底可用。
    所以这里检查的是「能力」，不是「某个厂商」。
    """
    from ..sources import sina as sina_src

    # 快照已建成的话，直接证明能力可用
    row = db.query_one("SELECT COUNT(*) AS c FROM market_snapshot")
    if row and (row["c"] or 0) > 0:
        stats = db.kv_get("snapshot_stats:a_share") or {}
        return f"已有快照 {row['c']} 条（来源 {stats.get('source', '未知')}）"

    errs = []
    for name, fn in (
        ("eastmoney", lambda: eastmoney.market_list("a_share", 1, 20)),
        ("sina", lambda: (sina_src.market_list_page("a_share", 1, 20), 0)),
    ):
        try:
            r = fn()
            rows = r[0] if isinstance(r, tuple) else r
            if rows:
                return f"可用（来源 {name}，首页 {len(rows)} 条）"
            errs.append(f"{name}: 空")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{name}: {exc}")
    raise RuntimeError("东财与新浪列表接口均不可用 — " + "; ".join(errs)[:150])


def check_fundamentals() -> str:
    data = market.get_fundamentals("600519.SH")
    latest = data.get("latest") or {}
    if not latest.get("report_date"):
        raise RuntimeError("缺少报告期")
    return f"{latest['report_date']} EPS={latest.get('eps')} ROE={latest.get('roe')}"


def check_search() -> str:
    hits = market.search("茅台", 5)
    if not hits:
        # 全新安装时快照还没建，中文名称搜索依赖快照，属正常初始状态
        row = db.query_one("SELECT COUNT(*) AS c FROM market_snapshot")
        if not row or not row["c"]:
            raise SkipCheck("全市场快照尚未建立，名称搜索依赖该数据")
        raise RuntimeError("快照已建立但搜索无结果")
    return f"搜索'茅台' -> {hits[0]['symbol']} {hits[0]['name']}"


def check_indicators_math() -> str:
    """已知答案校验，确保指标算得对而不只是算得出。"""
    from ..services import indicators as ta

    # MA：手算样例
    ma5 = ta.sma([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], 5)
    if abs(ma5[4] - 12.0) > 1e-9 or abs(ma5[10] - 18.0) > 1e-9:
        raise RuntimeError(f"MA 计算错误: {ma5[4]}, {ma5[10]}")
    # EMA：alpha = 2/(n+1)
    e = ta.ema([1, 2, 3, 4, 5], 3)
    if abs(e[4] - 4.0625) > 1e-9:
        raise RuntimeError(f"EMA 计算错误: {e[4]}")
    # 通达信 SMA(X,N,M)：Y = (M*X + (N-M)*Y')/N
    s = ta.cn_sma([10, 20, 30], 3, 1)
    if abs(s[1] - 13.3333333) > 1e-6:
        raise RuntimeError(f"通达信 SMA 计算错误: {s[1]}")
    # 布林带对称性
    b = ta.boll([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29], 20)
    if abs((b["upper"][-1] - b["mid"][-1]) - (b["mid"][-1] - b["lower"][-1])) > 1e-3:
        raise RuntimeError("布林带上下轨不对称")

    # 真实数据的内部一致性。
    # 注意：compute_all 返回的值经过 round(4)，所以容差必须 > 1e-4，
    # 用 1e-6 会误报（曾经因此把正确的计算判成错误）。
    tol = 3e-4
    bars = market.get_kline("600519.SH", "day", 120)
    ind = ta.compute_all(bars)
    if abs(ind["mid"][-1] - ind["ma20"][-1]) > tol:
        raise RuntimeError(f"BOLL 中轨({ind['mid'][-1]}) != MA20({ind['ma20'][-1]})")
    if abs(ind["macd"][-1] - 2 * (ind["dif"][-1] - ind["dea"][-1])) > tol:
        raise RuntimeError(
            f"MACD 柱({ind['macd'][-1]}) != 2*(DIF-DEA)="
            f"{2 * (ind['dif'][-1] - ind['dea'][-1])}"
        )
    if abs(ind["j"][-1] - (3 * ind["k"][-1] - 2 * ind["d"][-1])) > tol:
        raise RuntimeError("KDJ 的 J != 3K-2D")
    r = ind["rsi6"][-1]
    if r is not None and not (0 <= r <= 100):
        raise RuntimeError(f"RSI 越界: {r}")
    return "MA/EMA/通达信SMA/BOLL/MACD/KDJ/RSI 全部校验通过"


def check_backtest() -> str:
    from ..services import backtest as bt

    bars = market.get_kline("000001.SZ", "day", 400)
    if len(bars) < 60:
        raise RuntimeError("K线不足，无法回测")
    r = bt.run(bars, "ma_cross", {"fast": 5, "slow": 20}, 100000.0, symbol="000001.SZ")
    m = r.metrics
    if not m:
        raise RuntimeError("未产生指标")
    # 一致性：买卖笔数应匹配
    buys = sum(1 for t in r.trades if t["action"] == "buy")
    sells = sum(1 for t in r.trades if t["action"] == "sell")
    if buys != sells:
        raise RuntimeError(f"买卖不匹配: 买{buys} 卖{sells}")
    # 费用必须被扣除
    if m.get("total_fee", 0) <= 0 and m.get("trade_count", 0) > 0:
        raise RuntimeError("有交易但手续费为 0")
    return f"收益 {m['total_return']}% 交易 {m['trade_count']} 笔 费用 {m['total_fee']} 元"


def check_screener() -> str:
    from ..services import screener as sc

    r = sc.screen(
        [{"field": "pe", "op": "gt", "value": 0}, {"field": "pe", "op": "lt", "value": 25}],
        [], {}, "a_share", True, True, "amount DESC", 5,
    )
    if r["returned"] == 0:
        if r["total_candidates"] == 0:
            # 全新安装时快照本来就没建，这是正常初始状态，不该判失败
            raise SkipCheck("全市场快照尚未建立，请到「设置」页刷新")
        raise RuntimeError("有候选但无结果")
    return f"候选 {r['total_candidates']} 命中 {r['returned']}"


def check_ai_local() -> str:
    from ..services import ai as ai_svc

    ctx = ai_svc.gather_context("600519.SH")
    text = ai_svc._local_report(ctx)
    if len(text) < 300:
        raise RuntimeError(f"报告过短 ({len(text)} 字)")
    return f"本地规则引擎生成 {len(text)} 字"


def check_ai_remote() -> str:
    """只有配置了 API Key 才跑；未配置视为跳过。"""
    from ..services import ai as ai_svc

    if not (settings.ai_enabled and settings.ai_api_key):
        return "未配置 API Key，跳过（已使用本地引擎）"
    prompt = ai_svc.build_prompt(ai_svc.gather_context("600519.SH"))
    content = ai_svc._call_llm(prompt)
    if len(content) < 100:
        raise RuntimeError(f"返回过短: {len(content)} 字")
    return f"{settings.ai_model} 返回 {len(content)} 字"


def check_notify() -> str:
    from ..services import notify

    configured = notify.configured_channels()
    if not configured:
        return "未配置推送渠道（提醒仍会记录到历史，不会外发）"
    return "已配置: " + ", ".join(configured)


def check_scheduler() -> str:
    """验证调度配置。

    注意：命令行自检时进程里并没有跑调度器（只有 Web 服务会启动它），
    所以不能只看 jobs() 是否为空 —— 那会误报。这里真正构建一次调度器，
    确认所有 cron 表达式都能被解析注册，然后关掉。
    """
    from ..tasks import scheduler as sched

    if not settings.scheduler_enabled:
        return "调度器已禁用 (SL_SCHEDULER_ENABLED=0)"

    running = sched.jobs()
    if running:
        return f"运行中，已注册 {len(running)} 个任务"

    # 未运行（命令行自检）：临时构建验证
    try:
        s = sched.start()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"调度器无法启动: {exc}") from exc
    if s is None:
        raise RuntimeError("调度器启动返回空")
    try:
        jobs = sched.jobs()
        if not jobs:
            raise RuntimeError("调度器已启用但未注册任何任务")
        # 确认下次执行时间可解析（时区数据正常的间接证据）
        if not all(j.get("next_run") for j in jobs):
            raise RuntimeError("部分任务无法计算下次执行时间（时区数据可能异常）")
        return f"可正常构建，{len(jobs)} 个任务均可排程"
    finally:
        sched.stop()


def check_timezone() -> str:
    """定时任务必须在正确时区，否则盘后抓取会跑错时间。"""
    import datetime as dt

    now = dt.datetime.now()
    tz = time.strftime("%Z")
    if tz in ("UTC", "GMT") and settings.timezone == "Asia/Shanghai":
        raise RuntimeError(f"时区为 {tz}，定时任务会在错误时间触发（期望 CST）")
    return f"{tz} 当前 {now.strftime('%Y-%m-%d %H:%M:%S')}"


CHECKS: list[tuple[str, str, Callable[[], str], str]] = [
    ("数据目录可写", "storage", check_data_dir, "core"),
    ("数据库读写", "storage", check_db, "core"),
    ("时区设置", "storage", check_timezone, "core"),

    # 核心：能拿到行情就行，不绑定具体厂商
    ("行情获取(多源容错)", "source", check_quote_capability, "core"),
    # 逐源可靠性：只报告，不判失败（东财间歇性抽风是已知情况）
    ("数据源可靠性采样", "source", check_source_reliability, "optional"),
    # 以下都是「逐厂商」检查 —— 失败不代表系统不可用，
    # 因为上层有多源容错。真正的能力检查见 market 分组。
    ("东财·日线K线", "source", check_eastmoney_kline, "optional"),
    ("东财·全市场列表", "source", check_eastmoney_list, "optional"),
    ("腾讯·实时行情", "source", check_tencent_quote, "optional"),
    ("腾讯·港股K线", "source", check_tencent_kline, "optional"),
    ("新浪·实时行情", "source", check_sina_quote, "optional"),

    ("全市场列表(多源容错)", "market", check_market_list_capability, "core"),
    ("全市场行情覆盖", "market", check_markets, "core"),
    ("各市场K线", "market", check_kline_all_markets, "core"),
    ("财务数据", "market", check_fundamentals, "core"),
    ("标的搜索", "market", check_search, "optional"),

    ("指标数学校验", "engine", check_indicators_math, "core"),
    ("回测引擎", "engine", check_backtest, "core"),
    ("选股筛选器", "engine", check_screener, "core"),

    ("AI·本地引擎", "ai", check_ai_local, "core"),
    ("AI·大模型接口", "ai", check_ai_remote, "optional"),

    ("消息推送渠道", "ops", check_notify, "optional"),
    ("定时任务", "ops", check_scheduler, "core"),
]


def run_all(include_optional: bool = True, pacing: float = 0.35) -> dict[str, Any]:
    """执行全部自检。

    pacing: 联网检查之间的间隔（秒）。自检本身会连续发起十几个请求，
            不节流的话会把东财打成频控，从而**误报**数据源不可用
            —— 这是实测踩过的坑，不是理论担忧。
    """
    t0 = time.perf_counter()
    results: list[Check] = []
    network_groups = {"source", "market"}
    prev_group = None

    for name, group, fn, level in CHECKS:
        if level == "optional" and not include_optional:
            continue
        # 只在联网检查之间节流
        if group in network_groups and prev_group in network_groups and pacing > 0:
            time.sleep(pacing)
        c = _run(name, fn, level)
        c.group = group
        results.append(c)
        prev_group = group

    passed = sum(1 for c in results if c.ok and not c.skipped)
    skipped = [c for c in results if c.skipped]
    failed_core = [c for c in results if not c.ok and c.level == "core"]
    failed_opt = [c for c in results if not c.ok and c.level == "optional"]

    if failed_core:
        verdict = "fail"
        summary = f"核心检查失败 {len(failed_core)} 项，系统无法正常使用"
    elif failed_opt:
        verdict = "warn"
        summary = f"核心功能全部正常；{len(failed_opt)} 项可选检查未通过（不影响主流程）"
    elif skipped:
        verdict = "pass"
        summary = f"全部检查通过（{len(skipped)} 项因前置条件未满足被跳过）"
    else:
        verdict = "pass"
        summary = "全部检查通过，系统可正常使用"

    return {
        "verdict": verdict,
        "summary": summary,
        "passed": passed,
        "skipped": len(skipped),
        "total": len(results),
        "failed_core": len(failed_core),
        "failed_optional": len(failed_opt),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "env": _env_info(),
        "checks": [c.to_dict() for c in results],
    }


def _main() -> int:
    """命令行入口，便于在容器里直接执行。"""
    try:
        from app import db as _db
        _db.init_db()
    except Exception:  # noqa: BLE001
        pass

    r = run_all()
    icon = {"pass": "✅", "warn": "⚠️ ", "fail": "❌"}.get(r["verdict"], "?")
    line = "=" * 66

    print(line)
    print(f"  StockLab 部署自检  {icon} {r['verdict'].upper()}")
    print(line)

    e = r["env"]
    print(f"  Python {e['python']}  |  {e['machine']}  |  时区 {e['timezone']}")
    print(f"  数据目录 {e['data_dir']}")
    print(f"  数据源顺序 {', '.join(e['source_order'])}")
    print(line)

    group_names = {
        "storage": "存储与环境", "source": "数据源连通性", "market": "行情与财务",
        "engine": "计算引擎", "ai": "AI 简报", "ops": "运维",
    }
    cur = None
    for c in r["checks"]:
        g = c["group"]
        if g != cur:
            cur = g
            print(f"\n  ── {group_names.get(g, g)} ──")
        if c.get("skipped"):
            mark = "–"
        elif c["ok"]:
            mark = "✓"
        else:
            mark = "✗" if c["level"] == "core" else "○"
        pad = " " * max(0, 22 - len(c["name"]) * 2)
        print(f"   {mark} {c['name']}{pad} {c['detail']}  [{c['ms']}ms]")

    print()
    print(line)
    print(f"  {r['summary']}")
    skip_note = f"  跳过 {r['skipped']}" if r.get("skipped") else ""
    print(f"  通过 {r['passed']}/{r['total']}{skip_note}   总耗时 {r['elapsed_ms']}ms")
    print(line)

    if r["verdict"] == "fail":
        print("\n  修复建议：")
        for c in r["checks"]:
            if not c["ok"] and c["level"] == "core":
                print(f"   · {c['name']}: {c['detail']}")
        print("\n   多数数据源问题可通过调大 SL_MIN_REQUEST_INTERVAL（如 0.3）缓解；")
        print("   快照相关问题请到 Web 界面「设置」页点击「刷新全市场快照」。")
    return 0 if r["verdict"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(_main())
