"""
Scenario sweep — runs multiple param combinations through the backtest and
prints a ranked comparison table.

CLI:
    python -m src.backtest.scenarios
    python -m src.backtest.scenarios --days 60 --coins BTC ETH SOL WIF HYPE
"""
from __future__ import annotations

import argparse
import copy
import math
import time
from dataclasses import dataclass
from statistics import mean

from src.backtest.runner import BtResult, run
from src.config.settings import load_config, load_secrets
from src.storage.db import init_db, make_session_factory


@dataclass
class Scenario:
    name: str
    timeframe: str
    vol_z: float          # volume_zscore_min
    lookback: int         # breakout_lookback_candles
    tp_rr: float          # reward/risk ratio
    sl_mult: float        # sl_atr_multiplier
    max_pos: int          # max_concurrent_positions


SCENARIOS: list[Scenario] = [
    # ── Baseline ──────────────────────────────────────────────────────────────
    Scenario("S00-baseline",     "15m", 1.5, 20, 2.0, 1.5, 3),

    # ── Filter signal quality (reduce trade count, raise WR) ──────────────────
    Scenario("S01-filter-med",   "15m", 2.0, 30, 2.0, 1.5, 3),
    Scenario("S02-filter-high",  "15m", 2.5, 40, 2.0, 1.5, 2),

    # ── Better R:R (widen TP, keep entry same) ────────────────────────────────
    Scenario("S03-rr-3x",        "15m", 1.5, 20, 3.0, 1.5, 3),
    Scenario("S04-filter+rr",    "15m", 2.0, 30, 3.0, 1.5, 2),

    # ── Tighter SL (smaller loss per hit, needs higher WR) ────────────────────
    Scenario("S05-tight-sl",     "15m", 2.0, 30, 2.0, 1.0, 3),

    # ── Higher timeframe (fewer, stronger signals) ────────────────────────────
    Scenario("S06-1h-base",      "1h",  1.5, 20, 2.0, 1.5, 3),
    Scenario("S07-1h-filter",    "1h",  2.0, 30, 2.5, 1.5, 2),
    Scenario("S08-1h-highconv",  "1h",  2.5, 40, 3.0, 1.5, 2),

    # ── Serialised (no concurrent positions) ─────────────────────────────────
    Scenario("S09-serial",       "15m", 2.0, 30, 2.0, 1.5, 1),
]


def _apply(base_cfg, sc: Scenario):
    cfg = copy.deepcopy(base_cfg)
    cfg.strategy.timeframe = sc.timeframe
    cfg.strategy.volume_zscore_min = sc.vol_z
    cfg.strategy.breakout_lookback_candles = sc.lookback
    cfg.risk.tp_rr = sc.tp_rr
    cfg.risk.sl_atr_multiplier = sc.sl_mult
    cfg.risk.max_concurrent_positions = sc.max_pos
    return cfg


def _summarise(result: BtResult) -> dict:
    trades = [t for t in result.trades if t.exit_reason != ""]
    if not trades:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "pnl_pct": 0.0,
                "pf": 0.0, "dd": 0.0, "avg_w": 0.0, "avg_l": 0.0}

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = abs(sum(t.pnl_usd for t in losses))
    pf = gross_win / gross_loss if gross_loss else math.inf

    peak = result.initial_equity
    max_dd = 0.0
    for _, eq in result.equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        "n": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "pnl": total_pnl,
        "pnl_pct": total_pnl / result.initial_equity * 100,
        "pf": pf,
        "dd": max_dd,
        "avg_w": mean(t.pnl_usd for t in wins) if wins else 0.0,
        "avg_l": mean(t.pnl_usd for t in losses) if losses else 0.0,
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m src.backtest.scenarios")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--coins", nargs="+", default=None)
    args = p.parse_args()

    base_cfg = load_config()
    secrets = load_secrets()
    coins = args.coins or base_cfg.data.coins

    init_db(secrets.database_url)
    sf = make_session_factory(secrets.database_url)

    results: list[tuple[Scenario, dict]] = []

    print(f"\nScenario sweep | {', '.join(coins)} | {args.days}d | {len(SCENARIOS)} scenarios\n")

    for sc in SCENARIOS:
        cfg = _apply(base_cfg, sc)
        t0 = time.time()
        result = run(sf, coins, sc.timeframe, args.days, cfg)
        elapsed = time.time() - t0
        stats = _summarise(result)
        results.append((sc, stats))

        sign = "+" if stats["pnl"] >= 0 else ""
        print(
            f"  {sc.name:<22}  tf={sc.timeframe:<3}  "
            f"z={sc.vol_z}  lb={sc.lookback:>2}  rr={sc.tp_rr}  sl={sc.sl_mult}  pos={sc.max_pos}  "
            f"→  N={stats['n']:>4}  WR={stats['wr']:>5.1f}%  "
            f"PnL={sign}{stats['pnl']:>8,.0f}  PF={stats['pf']:>4.2f}  "
            f"DD={stats['dd']:>5.1f}%  [{elapsed:.0f}s]"
        )

    # Ranked table
    print(f"\n{'═'*110}")
    print(f"  RANKING  (sorted by Profit Factor desc)\n{'═'*110}")
    print(
        f"  {'#':>2}  {'Scenario':<22}  {'tf':<3}  "
        f"{'N':>5}  {'WR%':>6}  {'PnL$':>10}  {'PnL%':>7}  "
        f"{'PF':>5}  {'MaxDD%':>7}  {'AvgW':>7}  {'AvgL':>7}"
    )
    print(f"  {'─'*106}")

    ranked = sorted(results, key=lambda x: x[1]["pf"], reverse=True)
    for i, (sc, s) in enumerate(ranked, 1):
        sign = "+" if s["pnl"] >= 0 else ""
        print(
            f"  {i:>2}  {sc.name:<22}  {sc.timeframe:<3}  "
            f"{s['n']:>5}  {s['wr']:>6.1f}%  "
            f"{sign}{s['pnl']:>9,.0f}  {sign}{s['pnl_pct']:>6.1f}%  "
            f"{s['pf']:>5.2f}  {s['dd']:>6.1f}%  "
            f"{s['avg_w']:>+7.1f}  {s['avg_l']:>+7.1f}"
        )

    print(f"{'═'*110}\n")

    # Best scenario detail
    best_sc, best_s = ranked[0]
    print(f"Best: {best_sc.name}")
    print(f"  tf={best_sc.timeframe}  vol_z={best_sc.vol_z}  lookback={best_sc.lookback}")
    print(f"  tp_rr={best_sc.tp_rr}  sl_mult={best_sc.sl_mult}  max_pos={best_sc.max_pos}")
    print(f"  PnL={best_s['pnl']:+,.2f} ({best_s['pnl_pct']:+.2f}%)  "
          f"WR={best_s['wr']:.1f}%  PF={best_s['pf']:.2f}  DD={best_s['dd']:.1f}%\n")


if __name__ == "__main__":
    main()
