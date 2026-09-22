"""历史类比：这种情形以前发生过什么。

## 这是什么、不是什么

**不是预测**。它回答的是「历史上出现类似情形时，之后 5/10/20 个交易日
实际发生了什么」—— 把胜率、中位收益、最差情况摆出来，由你自己判断。

**为什么比"可能涨也可能跌"有用**：空话不提供任何决策信息；
而「63% 的日子上涨、中位数 +2.4%、但最差的一次 -32%」能直接告诉你
该用多大仓位、止损该设在哪。

## 三个必须处理的统计陷阱

1. **重叠窗口**。连续几天都触发信号时，它们的未来 20 日窗口大幅重叠，
   588 个样本点的有效独立性远低于 588。所以这里把「间隔小于持有期」的
   信号合并成一个**独立事件**，同时给出名义样本数和独立事件数 ——
   后者才是真实的证据量。

2. **样本内外的差异**。样本内 +5.95%、样本外 +3.95% 这种衰减很常见。
   只报总体均值会让人高估稳定性，所以按时间前后切分，两个都报。

3. **选股偏差**。本机库里只有少数标的，且多是长期向上的大盘股 ——
   任何"买入持有"的子集都会显示正收益。所以基准必须用**同一只股票
   自身的全部交易日**，而不是跨股票的混合均值。
"""
from __future__ import annotations

import logging
import math
import statistics as st
from typing import Any

log = logging.getLogger("stocklab.analogs")

HORIZONS = (5, 10, 20)
MIN_FOR_STATS = 8          # 少于这么多独立事件就不给统计


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _pos(closes: list[float], i: int, n: int = 250) -> float | None:
    seg = closes[max(0, i - n + 1): i + 1]
    if len(seg) < 30:
        return None
    hi, lo = max(seg), min(seg)
    if hi <= lo:
        return None
    return (closes[i] - lo) / (hi - lo) * 100


def _phase(closes: list[float], i: int) -> str | None:
    if i < 60:
        return None
    ma5 = sum(closes[i - 4:i + 1]) / 5
    ma20 = sum(closes[i - 19:i + 1]) / 20
    ma60 = sum(closes[i - 59:i + 1]) / 60
    if ma5 > ma20 > ma60:
        return "上升通道"
    if ma5 < ma20 < ma60:
        return "下降通道"
    return "震荡/交织"


def _bucket(v: float | None, edges=(30, 70)) -> str | None:
    if v is None:
        return None
    return "低" if v <= edges[0] else ("高" if v >= edges[1] else "中")


def _features(bars: list[dict]) -> list[dict[str, Any]]:
    """逐日计算可复现的特征（只用 K线，不用资金流 —— 后者没有历史）。"""
    closes = [_f(b.get("close")) or 0 for b in bars]
    vols = [_f(b.get("volume")) or 0 for b in bars]
    out: list[dict[str, Any]] = []
    rets = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets[i] = (closes[i] / closes[i - 1] - 1) * 100
    for i in range(len(closes)):
        if i < 61:
            continue
        sd = st.pstdev(rets[i - 60:i]) if len(rets[i - 60:i]) >= 20 else 0.0
        if sd <= 0:
            continue
        vr = None
        if i >= 21 and vols[i]:
            avg = sum(vols[i - 20:i]) / 20
            if avg > 0:
                vr = vols[i] / avg
        out.append({
            "i": i,
            "date": bars[i].get("date"),
            "z": rets[i] / sd,
            "vol_ratio": vr,
            "pos": _pos(closes, i),
            "phase": _phase(closes, i),
            "close": closes[i],
        })
    return out


def _forward(closes: list[float], i: int, h: int) -> float | None:
    if i + h >= len(closes) or closes[i] <= 0:
        return None
    return (closes[i + h] / closes[i] - 1) * 100


def _independent_days(idx: list[int], gap: int) -> int:
    """把间隔小于 gap 的信号合并成独立事件，返回事件数。

    这是重叠窗口的修正：连续 5 天触发信号，它们的 20 日窗口几乎重合，
    算 5 个样本等于把同一件事数了 5 遍。
    """
    if not idx:
        return 0
    idx = sorted(idx)
    n = 1
    last = idx[0]
    for x in idx[1:]:
        if x - last >= gap:
            n += 1
        last = x
    return n


def _describe(vals: list[float], indep: int) -> dict[str, Any]:
    if not vals:
        return {"count": 0, "independent": 0}
    v = sorted(vals)
    n = len(v)

    def q(p: float) -> float:
        k = (n - 1) * p
        lo = int(math.floor(k))
        hi = min(lo + 1, n - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)

    return {
        "count": n,
        "independent": indep,
        "mean": round(st.mean(v), 2),
        "median": round(v[n // 2], 2),
        "win_rate": round(sum(1 for x in v if x > 0) / n * 100, 1),
        "p10": q(0.10), "p25": q(0.25), "p75": q(0.75), "p90": q(0.90),
        "worst": round(v[0], 2), "best": round(v[-1], 2),
    }


def analyze(symbol: str, bars: list[dict], today: dict | None = None,
            direction: str | None = None) -> dict[str, Any]:
    """找历史上"像今天这样"的日子，统计之后发生了什么。

    today: 异动分析给出的 detect 结果（用它的 z 分数和方向）
    """
    if not bars or len(bars) < 250:
        return {"ok": False, "reason": f"K线只有 {len(bars)} 根，不足 250 根，无法做历史类比"}

    feats = _features(bars)
    if len(feats) < 100:
        return {"ok": False, "reason": "可用的历史特征点太少"}

    closes = [_f(b.get("close")) or 0 for b in bars]
    last = feats[-1]
    max_h = max(HORIZONS)

    # 今天的方向与强度
    z_today = _f((today or {}).get("z_score")) or last["z"]
    dir_up = (direction or ("up" if z_today >= 0 else "down")) == "up"
    pos_today = _bucket(last.get("pos"))
    phase_today = last.get("phase")

    # 历史候选：留出 max_h 根给未来收益，且方向一致
    cands = [f for f in feats[:-max_h]
             if (f["z"] >= 0) == dir_up and abs(f["z"]) >= 1.0]

    # 三档相似度，从严到宽，凑够样本就用
    tiers = [
        ("严格", lambda f: abs(f["z"] - z_today) <= 0.75
         and _bucket(f.get("pos")) == pos_today and f.get("phase") == phase_today),
        ("中等", lambda f: abs(f["z"] - z_today) <= 1.5
         and _bucket(f.get("pos")) == pos_today),
        ("宽松", lambda f: abs(f["z"]) >= 1.0),
    ]

    chosen = None
    chosen_tier = None
    for name, pred in tiers:
        sel = [f for f in cands if pred(f)]
        if len(sel) >= MIN_FOR_STATS:
            chosen, chosen_tier = sel, name
            break
    if chosen is None:
        chosen, chosen_tier = cands, "宽松"

    if len(chosen) < MIN_FOR_STATS:
        return {
            "ok": False,
            "reason": (f"这只股票历史上只找到 {len(chosen)} 个同向异动样本，"
                       f"少于 {MIN_FOR_STATS} 个，样本量不足以做统计。"),
            "direction": "上涨异动" if dir_up else "下跌异动",
        }

    # 基准：同一只股票的全部交易日（避免跨股票混合带来的选股偏差）
    baseline_idx = [f["i"] for f in feats[:-max_h]]
    sig_idx = [f["i"] for f in chosen]

    horizons: dict[str, Any] = {}
    for h in HORIZONS:
        vals = [v for v in (_forward(closes, f["i"], h) for f in chosen) if v is not None]
        bvals = [v for v in (_forward(closes, i, h) for i in baseline_idx) if v is not None]
        horizons[str(h)] = {
            "signal": _describe(vals, _independent_days(sig_idx, h)),
            "baseline": _describe(bvals, _independent_days(baseline_idx, h)),
        }
        s, b = horizons[str(h)]["signal"], horizons[str(h)]["baseline"]
        if s.get("mean") is not None and b.get("mean") is not None:
            horizons[str(h)]["edge_mean"] = round(s["mean"] - b["mean"], 2)
            horizons[str(h)]["edge_win"] = round(s["win_rate"] - b["win_rate"], 1)

    # 样本内外切分：检验这个优势稳不稳定
    oos: dict[str, Any] = {}
    mid_date = feats[len(feats) // 2]["date"]
    for label, cond in (("in_sample", lambda f: str(f["date"]) <= str(mid_date)),
                        ("out_sample", lambda f: str(f["date"]) > str(mid_date))):
        sub = [f for f in chosen if cond(f)]
        vals = [v for v in (_forward(closes, f["i"], 20) for f in sub) if v is not None]
        oos[label] = _describe(vals, _independent_days([f["i"] for f in sub], 20))

    # 最坏情况：给仓位和止损提供依据
    worst20 = [v for v in (_forward(closes, f["i"], 20) for f in chosen) if v is not None]
    tail = None
    if worst20:
        srt = sorted(worst20)
        tail = {
            "loss_gt_10": sum(1 for x in srt if x < -10),
            "loss_gt_20": sum(1 for x in srt if x < -20),
            "worst": round(srt[0], 2),
            "worst_date": next((f["date"] for f in chosen
                                if _forward(closes, f["i"], 20) == srt[0]), None),
        }

    return {
        "ok": True,
        "direction": "上涨异动" if dir_up else "下跌异动",
        "today_z": round(z_today, 2),
        "today_pos": pos_today,
        "today_phase": phase_today,
        "tier": chosen_tier,
        "events": len(chosen),
        "span": f"{chosen[0]['date']} ~ {chosen[-1]['date']}",
        "split_date": mid_date,
        "horizons": horizons,
        "oos": oos,
        "tail": tail,
        "samples": [
            {"date": f["date"], "z": round(f["z"], 2),
             "pos": None if f["pos"] is None else round(f["pos"], 1),
             "phase": f["phase"],
             "fwd": {str(h): (None if (v := _forward(closes, f["i"], h)) is None
                              else round(v, 2)) for h in HORIZONS}}
            for f in sorted(chosen, key=lambda x: -(abs(x["z"])))[:12]
        ],
        "caveat": (
            "这是**历史统计**，不是预测。三点务必注意："
            "① 同向异动的样本之间存在重叠窗口，所以「独立事件数」比「样本数」小得多，"
            "后者才是真实的证据量；② 样本内与样本外的差异说明这个优势可能衰减；"
            "③ 本机可用的历史标的有限、且多为长期向上的大盘股，"
            "结论不能外推到小盘股或其它时期。"
        ),
    }
