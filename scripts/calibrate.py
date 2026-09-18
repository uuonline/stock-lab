#!/usr/bin/env python3
"""重新标定综合评分的结论档位。

为什么需要这个：
    综合评分是多个子项的平均，天然向中间收敛。如果档位阈值拍脑袋定，
    很容易出现「所有股票都是中性」或「所有股票都是看多」—— 评分就失去意义。
    所以档位必须按**真实分数分布的分位数**来划。

做法：
    对全市场按成交额分层抽样（默认 150 只，覆盖大小盘），逐只算综合评分，
    取 p15 / p35 / p65 / p85 作为五档分界，然后把结果写回
    app/services/panel.py 的 VERDICT_BANDS。

用法：
    python scripts/calibrate.py                # 抽样 150 只
    python scripts/calibrate.py --n 300        # 抽样更多（更准，更慢）
    python scripts/calibrate.py --dry-run      # 只打印，不改文件
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                    # noqa: E402
from app.services import indicators as ta, panel      # noqa: E402
from app.sources import market                        # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    a = sorted(values)
    k = (len(a) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(a) - 1)
    return a[f] + (a[c] - a[f]) * (k - f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150, help="抽样数量（默认 150）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写回文件")
    args = ap.parse_args()

    db.init_db()
    rows = db.query(
        "SELECT symbol, name FROM market_snapshot WHERE asset_type='stock' "
        "AND amount IS NOT NULL ORDER BY amount DESC"
    )
    if not rows:
        print("✗ 全市场快照为空，请先在 Web 界面「设置」页刷新快照")
        return 1

    step = max(1, len(rows) // args.n)
    sample = [dict(r) for r in rows[::step][: args.n]]
    print(f"全市场 {len(rows)} 只 → 抽样 {len(sample)} 只（每 {step} 只取 1）")
    print("计算中 ...")

    scores: list[float] = []
    t0 = time.time()
    for i, r in enumerate(sample):
        try:
            bars = market.get_kline(r["symbol"], "day", 260)
            if not bars or len(bars) < 60:
                continue
            q = market.get_quotes([r["symbol"]]).get(r["symbol"]) or {}
            ctx = {
                "symbol": r["symbol"], "quote": q, "bars": bars,
                "tech": ta.latest_snapshot(bars),
            }
            scores.append(panel.composite_score(ctx)["score"])
        except Exception:  # noqa: BLE001
            continue
        if (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(sample)}  已用 {time.time() - t0:.0f}s")

    if len(scores) < 30:
        print(f"✗ 有效样本仅 {len(scores)} 只，不足以标定（需 ≥30）")
        return 1

    p15, p35, p65, p85 = (percentile(scores, p) for p in (15, 35, 65, 85))
    print()
    print(f"有效样本 {len(scores)} 只，用时 {time.time() - t0:.0f}s")
    print(f"  分布: min={min(scores):.2f}  p15={p15:.2f}  p35={p35:.2f}  "
          f"中位={percentile(scores, 50):.2f}  p65={p65:.2f}  p85={p85:.2f}  max={max(scores):.2f}")
    print()
    print("新档位:")
    print(f"  看多     >= {p85:.2f}   (前 15%)")
    print(f"  谨慎看多  >= {p65:.2f}   (前 35%)")
    print(f"  中性     >= {p35:.2f}   (中间 30%)")
    print(f"  谨慎看空  >= {p15:.2f}   (后 35%)")
    print(f"  看空     <  {p15:.2f}   (后 15%)")

    if args.dry_run:
        print("\n(--dry-run，未修改文件)")
        return 0

    target = ROOT / "app" / "services" / "panel.py"
    src = target.read_text(encoding="utf-8")
    new_bands = (
        "VERDICT_BANDS = (\n"
        f'    ({p85:.2f}, "看多"),        # 前 15%\n'
        f'    ({p65:.2f}, "谨慎看多"),    # 前 35%\n'
        f'    ({p35:.2f}, "中性"),        # 中间 30%\n'
        f'    ({p15:.2f}, "谨慎看空"),    # 后 35%\n'
        '    (0.00, "看空"),        # 后 15%\n'
        ")"
    )
    new_src, n = re.subn(r"VERDICT_BANDS = \([^)]*\)", new_bands, src, count=1)
    if n != 1:
        print("✗ 未能定位 VERDICT_BANDS，请手动更新")
        return 1

    today = time.strftime("%Y-%m-%d")
    new_src = re.sub(
        r'CALIBRATION_NOTE = "[^"]*"',
        f'CALIBRATION_NOTE = "档位基于全市场 {len(scores)} 只分层抽样标定（{today}）"',
        new_src, count=1,
    )
    target.write_text(new_src, encoding="utf-8")
    print(f"\n✓ 已写入 {target.relative_to(ROOT)}")
    print("  重启服务后生效： ./scripts/serve.sh restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
