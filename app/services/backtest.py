"""策略回测引擎（日线，事件驱动）。

严格按 A股 交易规则建模，避免"回测很美、实盘很惨"：
  * T+1：当日买入的股票当日不可卖出
  * 涨跌停：涨停买不进、跌停卖不出（按收盘价触及涨跌停判断）
  * 佣金：双边万 2.5，单笔最低 5 元
  * 印花税：卖出单边千 0.5
  * 过户费：沪市双边 0.001%
  * 滑点：双边各 0.05%（可配）
  * 整手交易：买入按 100 股向下取整

信号在 T 日收盘产生，T+1 日开盘成交（避免用未来数据）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from . import indicators as ta


# ---------------- 交易成本 ----------------

@dataclass
class CostModel:
    commission_rate: float = 0.00025   # 万 2.5
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005     # 印花税，仅卖出
    transfer_fee_rate: float = 0.00001 # 过户费，沪市双边
    slippage_rate: float = 0.0005
    is_sh: bool = True

    def buy_cost(self, amount: float) -> float:
        fee = max(amount * self.commission_rate, self.min_commission)
        if self.is_sh:
            fee += amount * self.transfer_fee_rate
        return fee

    def sell_cost(self, amount: float) -> float:
        fee = max(amount * self.commission_rate, self.min_commission)
        fee += amount * self.stamp_tax_rate
        if self.is_sh:
            fee += amount * self.transfer_fee_rate
        return fee


# ---------------- 策略 ----------------

def _need(bars: list[dict], n: int) -> bool:
    return len(bars) >= n


def strategy_ma_cross(bars: list[dict], fast: int = 5, slow: int = 20, **_: Any) -> np.ndarray:
    closes = [b["close"] for b in bars]
    f = ta.arr_or_nan(ta.sma(closes, fast))
    s = ta.arr_or_nan(ta.sma(closes, slow))
    sig = np.zeros(len(bars), dtype=bool)
    for i in range(len(bars)):
        if math.isnan(f[i]) or math.isnan(s[i]):
            continue
        sig[i] = f[i] > s[i]
    return sig


def strategy_macd(bars: list[dict], fast: int = 12, slow: int = 26, signal: int = 9, **_: Any) -> np.ndarray:
    closes = [b["close"] for b in bars]
    m = ta.macd(closes, fast, slow, signal)
    dif = ta.arr_or_nan(np.array([np.nan if v is None else v for v in m["dif"]]))
    dea = ta.arr_or_nan(np.array([np.nan if v is None else v for v in m["dea"]]))
    sig = np.zeros(len(bars), dtype=bool)
    for i in range(len(bars)):
        if math.isnan(dif[i]) or math.isnan(dea[i]):
            continue
        sig[i] = dif[i] > dea[i]
    return sig


def strategy_kdj(bars: list[dict], n: int = 9, **_: Any) -> np.ndarray:
    k = ta.kdj([b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars], n)
    kk = ta.arr_or_nan(np.array([np.nan if v is None else v for v in k["k"]]))
    dd = ta.arr_or_nan(np.array([np.nan if v is None else v for v in k["d"]]))
    sig = np.zeros(len(bars), dtype=bool)
    for i in range(len(bars)):
        if math.isnan(kk[i]) or math.isnan(dd[i]):
            continue
        sig[i] = kk[i] > dd[i] and kk[i] < 80
    return sig


def strategy_rsi_reversal(
    bars: list[dict], period: int = 6, buy_below: float = 30, sell_above: float = 70, **_: Any
) -> np.ndarray:
    closes = [b["close"] for b in bars]
    r = ta.rsi(closes, (period,))[f"rsi{period}"]
    rr = ta.arr_or_nan(np.array([np.nan if v is None else v for v in r]))
    sig = np.zeros(len(bars), dtype=bool)
    holding = False
    for i in range(len(bars)):
        if math.isnan(rr[i]):
            continue
        if not holding and rr[i] < buy_below:
            holding = True
        elif holding and rr[i] > sell_above:
            holding = False
        sig[i] = holding
    return sig


def strategy_boll_breakout(
    bars: list[dict], n: int = 20, k: float = 2.0, exit_mid: bool = True, **_: Any
) -> np.ndarray:
    closes = [b["close"] for b in bars]
    b = ta.boll(closes, n, k)
    up = ta.arr_or_nan(np.array([np.nan if v is None else v for v in b["upper"]]))
    mid = ta.arr_or_nan(np.array([np.nan if v is None else v for v in b["mid"]]))
    sig = np.zeros(len(bars), dtype=bool)
    holding = False
    for i in range(len(bars)):
        if math.isnan(up[i]):
            continue
        c = closes[i]
        if not holding and c > up[i]:
            holding = True
        elif holding:
            if exit_mid and c < mid[i]:
                holding = False
            elif not exit_mid and c < up[i] * 0.97:
                holding = False
        sig[i] = holding
    return sig


def strategy_momentum(bars: list[dict], lookback: int = 20, threshold: float = 0.0, **_: Any) -> np.ndarray:
    closes = [b["close"] for b in bars]
    sig = np.zeros(len(bars), dtype=bool)
    for i in range(lookback, len(bars)):
        if closes[i - lookback]:
            ret = closes[i] / closes[i - lookback] - 1.0
            sig[i] = ret > threshold
    return sig


def strategy_volume_breakout(
    bars: list[dict], vol_mult: float = 2.0, ma: int = 20, **_: Any
) -> np.ndarray:
    closes = [b["close"] for b in bars]
    vols = [b["volume"] or 0 for b in bars]
    vma = ta.arr_or_nan(ta.sma(vols, ma))
    cma = ta.arr_or_nan(ta.sma(closes, ma))
    sig = np.zeros(len(bars), dtype=bool)
    holding = False
    for i in range(len(bars)):
        if math.isnan(vma[i]) or math.isnan(cma[i]) or not vma[i]:
            continue
        up = closes[i] > closes[i - 1] if i > 0 else False
        if not holding and vols[i] > vma[i] * vol_mult and up and closes[i] > cma[i]:
            holding = True
        elif holding and closes[i] < cma[i]:
            holding = False
        sig[i] = holding
    return sig


def strategy_buy_hold(bars: list[dict], **_: Any) -> np.ndarray:
    return np.ones(len(bars), dtype=bool)


STRATEGIES: dict[str, dict[str, Any]] = {
    "buy_hold": {
        "name": "买入持有",
        "fn": strategy_buy_hold,
        "params": {},
        "desc": "首日买入并一直持有，作为基准参照",
    },
    "ma_cross": {
        "name": "双均线",
        "fn": strategy_ma_cross,
        "params": {"fast": 5, "slow": 20},
        "desc": "快线上穿慢线买入，下穿卖出",
    },
    "macd": {
        "name": "MACD",
        "fn": strategy_macd,
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "desc": "DIF 上穿 DEA 持有，下穿空仓",
    },
    "kdj": {
        "name": "KDJ",
        "fn": strategy_kdj,
        "params": {"n": 9},
        "desc": "K 上穿 D 且 K<80 持有",
    },
    "rsi_reversal": {
        "name": "RSI 超卖反转",
        "fn": strategy_rsi_reversal,
        "params": {"period": 6, "buy_below": 30, "sell_above": 70},
        "desc": "RSI 低于阈值买入，高于阈值卖出",
    },
    "boll_breakout": {
        "name": "布林突破",
        "fn": strategy_boll_breakout,
        "params": {"n": 20, "k": 2.0, "exit_mid": True},
        "desc": "突破上轨买入，跌破中轨卖出",
    },
    "momentum": {
        "name": "动量",
        "fn": strategy_momentum,
        "params": {"lookback": 20, "threshold": 0.0},
        "desc": "N 日涨幅为正则持有",
    },
    "volume_breakout": {
        "name": "放量突破",
        "fn": strategy_volume_breakout,
        "params": {"vol_mult": 2.0, "ma": 20},
        "desc": "放量上涨且站上均线买入，跌破均线卖出",
    },
}


# ---------------- 回测主循环 ----------------

@dataclass
class BTResult:
    metrics: dict[str, Any] = field(default_factory=dict)
    equity: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    signals: list[bool] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)


def run(
    bars: list[dict],
    strategy: str = "ma_cross",
    params: dict[str, Any] | None = None,
    initial_cash: float = 100000.0,
    cost: CostModel | None = None,
    position_size: float = 1.0,
    symbol: str = "",
    allow_fractional: bool = False,
) -> BTResult:
    """执行回测。

    position_size:     每次买入使用的资金比例 (0~1)
    allow_fractional:  True 时允许不足一手的零股（方便回测高价股），
                       默认 False，严格遵守 A股整手规则。
    """
    if not bars or len(bars) < 30:
        raise ValueError("K线数据不足（至少 30 根）")

    strat = STRATEGIES.get(strategy)
    if strat is None:
        raise ValueError(f"未知策略: {strategy}")
    p = dict(strat["params"])
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    cost = cost or CostModel(is_sh=symbol.upper().endswith(".SH"))

    dates = [b["date"] for b in bars]
    opens = np.array([b.get("open") or b.get("close") or 0.0 for b in bars], dtype=float)
    closes = np.array([b.get("close") or 0.0 for b in bars], dtype=float)
    highs = np.array([b.get("high") or 0.0 for b in bars], dtype=float)
    lows = np.array([b.get("low") or 0.0 for b in bars], dtype=float)
    prev_closes = np.concatenate([[closes[0]], closes[:-1]])

    try:
        target = strat["fn"](bars, **p)
    except TypeError as exc:
        raise ValueError(f"策略参数错误: {exc}") from exc
    target = np.asarray(target, dtype=bool)
    if len(target) != len(bars):
        target = np.resize(target, len(bars))

    cash = float(initial_cash)
    shares = 0
    buy_date_idx: int | None = None
    trades: list[dict] = []
    equity: list[dict] = []
    sig_list: list[bool] = []
    warnings: list[str] = []
    insufficient_lot_noted = False

    # T日收盘信号 -> T+1 开盘执行
    pending: str | None = None

    for i in range(len(bars)):
        # 1) 执行前一日产生的指令（今日开盘价成交）
        if pending == "buy" and shares == 0:
            px = opens[i]
            if px > 0 and not _limit_up(px, prev_closes[i], symbol):
                exec_px = px * (1 + cost.slippage_rate)
                budget = cash * position_size
                if allow_fractional:
                    qty = int(budget / exec_px)
                else:
                    qty = int(budget / (exec_px * 100)) * 100
                if qty <= 0 and not insufficient_lot_noted:
                    min_cost = exec_px * 100
                    warnings.append(
                        f"资金不足：初始资金 {initial_cash:,.0f} 元买不起 1 手"
                        f"（约需 {min_cost:,.0f} 元），全程空仓。"
                        f"请提高初始资金至 {math.ceil(min_cost / 10000) * 10000:,.0f} 元以上，"
                        f"或开启「允许零股」模式。"
                    )
                    insufficient_lot_noted = True
                if qty > 0:
                    amount = qty * exec_px
                    fee = cost.buy_cost(amount)
                    if amount + fee <= cash:
                        cash -= amount + fee
                        shares = qty
                        buy_date_idx = i
                        trades.append({
                            "date": dates[i], "action": "buy",
                            "price": round(float(exec_px), 3),
                            "shares": qty, "amount": round(float(amount), 2),
                            "fee": round(float(fee), 2),
                            "cash_after": round(float(cash), 2), "reason": "信号买入",
                        })
        elif pending == "sell" and shares > 0:
            # T+1：买入当日不可卖
            if buy_date_idx is None or i > buy_date_idx:
                px = opens[i]
                if px > 0 and not _limit_down(px, prev_closes[i], symbol):
                    exec_px = px * (1 - cost.slippage_rate)
                    amount = shares * exec_px
                    fee = cost.sell_cost(amount)
                    cash += amount - fee
                    pnl = None
                    buy_trade = next((t for t in reversed(trades) if t["action"] == "buy"), None)
                    if buy_trade:
                        cost_basis = buy_trade["amount"] + buy_trade["fee"]
                        pnl = round(float(amount - fee - cost_basis), 2)
                    trades.append({
                        "date": dates[i], "action": "sell",
                        "price": round(float(exec_px), 3),
                        "shares": shares, "amount": round(float(amount), 2),
                        "fee": round(float(fee), 2),
                        "cash_after": round(float(cash), 2), "pnl": pnl,
                        "hold_days": (i - buy_date_idx) if buy_date_idx is not None else None,
                        "reason": "信号卖出",
                    })
                    shares = 0
                    buy_date_idx = None
        pending = None

        # 2) 按今日收盘价计算信号，明日执行
        if target[i]:
            if shares == 0:
                pending = "buy"
        else:
            if shares > 0:
                pending = "sell"

        equity.append({
            "date": dates[i],
            "equity": round(float(cash + shares * closes[i]), 2),
            "cash": round(float(cash), 2),
            "position_value": round(float(shares * closes[i]), 2),
            "close": round(float(closes[i]), 3),
        })
        sig_list.append(bool(target[i]))

    # 末日强制清仓
    if shares > 0:
        px = closes[-1] * (1 - cost.slippage_rate)
        amount = shares * px
        fee = cost.sell_cost(amount)
        cash += amount - fee
        buy_trade = next((t for t in reversed(trades) if t["action"] == "buy"), None)
        pnl = None
        if buy_trade:
            pnl = round(float(amount - fee - (buy_trade["amount"] + buy_trade["fee"])), 2)
        trades.append({
            "date": dates[-1], "action": "sell", "price": round(float(px), 3),
            "shares": shares, "amount": round(float(amount), 2),
            "fee": round(float(fee), 2),
            "cash_after": round(float(cash), 2), "pnl": pnl,
            "hold_days": (len(bars) - 1 - buy_date_idx) if buy_date_idx is not None else None,
            "reason": "末日清仓",
        })
        shares = 0
        if equity:
            equity[-1]["equity"] = round(float(cash), 2)
            equity[-1]["position_value"] = 0.0

    metrics = _metrics(equity, trades, initial_cash, bars, cost)
    if warnings:
        metrics["warnings"] = warnings
    return BTResult(metrics=metrics, equity=equity, trades=trades, signals=sig_list, dates=dates)


def _limit_up(price: float, prev_close: float, symbol: str) -> bool:
    """是否涨停（粗略：主板 10%，创业板/科创板 20%，北交所 30%）。"""
    if not prev_close:
        return False
    pct = (price - prev_close) / prev_close
    limit = _limit_pct(symbol)
    return pct >= limit - 0.005


def _limit_down(price: float, prev_close: float, symbol: str) -> bool:
    if not prev_close:
        return False
    pct = (price - prev_close) / prev_close
    limit = _limit_pct(symbol)
    return pct <= -limit + 0.005


def _limit_pct(symbol: str) -> float:
    code, _, mkt = symbol.upper().rpartition(".")
    if mkt == "HK":
        return 1.0  # 港股无涨跌停
    if mkt == "BJ":
        return 0.30
    if code.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def _metrics(
    equity: list[dict],
    trades: list[dict],
    initial_cash: float,
    bars: list[dict],
    cost: CostModel,
) -> dict[str, Any]:
    if not equity:
        return {}
    eq = np.array([e["equity"] for e in equity], dtype=float)
    final = float(eq[-1])
    total_ret = final / initial_cash - 1.0

    # 年化（按 244 交易日）
    n = len(eq)
    years = n / 244.0
    annual = (final / initial_cash) ** (1 / years) - 1 if years > 0 and final > 0 else 0.0

    # 最大回撤
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (eq - peak) / peak, 0.0)
    max_dd = float(dd.min()) if len(dd) else 0.0

    # 日收益
    rets = np.diff(eq) / eq[:-1] if n > 1 else np.array([0.0])
    rets = rets[np.isfinite(rets)]
    if len(rets) > 1 and rets.std(ddof=1) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(244))
    else:
        sharpe = 0.0
    downside = rets[rets < 0]
    if len(downside) > 1 and downside.std(ddof=1) > 0:
        sortino = float(rets.mean() / downside.std(ddof=1) * math.sqrt(244))
    else:
        sortino = 0.0

    # 交易统计
    sells = [t for t in trades if t["action"] == "sell" and t.get("pnl") is not None]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    win_rate = len(wins) / len(sells) if sells else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_hold = (
        sum(t.get("hold_days") or 0 for t in sells) / len(sells) if sells else 0.0
    )
    total_fee = sum(t.get("fee") or 0 for t in trades)

    # 基准：同期买入持有
    closes = [b.get("close") for b in bars if b.get("close")]
    bench_ret = (closes[-1] / closes[0] - 1.0) if len(closes) > 1 and closes[0] else 0.0

    # 持仓暴露
    exposure = (
        sum(1 for e in equity if e["position_value"] > 0) / len(equity) if equity else 0.0
    )

    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final, 2),
        "total_return": round(total_ret * 100, 2),
        "annual_return": round(annual * 100, 2),
        "benchmark_return": round(bench_ret * 100, 2),
        "excess_return": round((total_ret - bench_ret) * 100, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round((annual / abs(max_dd)) if max_dd else 0.0, 3),
        "trade_count": len(sells),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate * 100, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "avg_hold_days": round(avg_hold, 1),
        "total_fee": round(total_fee, 2),
        "exposure": round(exposure * 100, 2),
        "bars": len(bars),
        "start": bars[0]["date"] if bars else "",
        "end": bars[-1]["date"] if bars else "",
    }


def compare_strategies(
    bars: list[dict],
    initial_cash: float = 100000.0,
    symbol: str = "",
    only: list[str] | None = None,
    allow_fractional: bool = False,
) -> list[dict]:
    """用默认参数跑所有策略并排序。"""
    out = []
    for key, meta in STRATEGIES.items():
        if only and key not in only:
            continue
        try:
            r = run(
                bars, key, None, initial_cash, symbol=symbol,
                allow_fractional=allow_fractional,
            )
            out.append({
                "strategy": key,
                "name": meta["name"],
                "desc": meta["desc"],
                **{k: v for k, v in r.metrics.items()},
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"strategy": key, "name": meta["name"], "error": str(exc)})
    out.sort(key=lambda x: x.get("total_return", -999), reverse=True)
    return out
