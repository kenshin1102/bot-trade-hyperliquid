"""
Scenario sweep — runs multiple param combinations through the backtest and
prints a ranked comparison table plus writes a JSON audit log.

CLI:
    python -m src.backtest.scenarios
    python -m src.backtest.scenarios --days 60 --coins BTC ETH SOL WIF HYPE
    python -m src.backtest.scenarios --days 60 --htf none   # no HTF filter
    python -m src.backtest.scenarios --days 60 --htf 4h     # 4h trend filter only
    python -m src.backtest.scenarios --days 60 --htf all    # generate all variants
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from src.backtest.runner import BtResult, CandleCache, load_candle_cache, run
from src.config.settings import load_config, load_secrets
from src.storage.db import init_db, make_session_factory


@dataclass
class Scenario:
    name: str
    timeframe: str
    vol_z: float
    lookback: int
    tp_rr: float
    sl_mult: float
    max_pos: int
    htf_filter: list[str] = field(default_factory=list)


# ── base scenario definitions ──────────────────────────────────────────────────

_BASE_PARAMS = [
    # (name,             tf,    vol_z, lb, rr,  sl,  pos)
    ("S00-baseline",    "15m", 1.5,  20, 2.0, 1.5, 3),
    ("S01-filter-med",  "15m", 2.0,  30, 2.0, 1.5, 3),
    ("S02-filter-high", "15m", 2.5,  40, 2.0, 1.5, 2),
    ("S03-rr-3x",       "15m", 1.5,  20, 3.0, 1.5, 3),
    ("S04-filter+rr",   "15m", 2.0,  30, 3.0, 1.5, 2),
    ("S05-tight-sl",    "15m", 2.0,  30, 2.0, 1.0, 3),
    ("S06-1h-base",     "1h",  1.5,  20, 2.0, 1.5, 3),
    ("S07-1h-filter",   "1h",  2.0,  30, 2.5, 1.5, 2),
    ("S08-1h-highconv", "1h",  2.5,  40, 3.0, 1.5, 2),
    ("S09-serial",      "15m", 2.0,  30, 2.0, 1.5, 1),
]

_HTF_VARIANTS: list[tuple[str, list[str]]] = [
    ("",      []),           # no HTF filter
    ("-4h",   ["4h"]),       # 4h trend filter
    ("-4h1d", ["4h", "1d"]), # 4h + daily trend filter
]


def _build_scenarios(htf_mode: str) -> list[Scenario]:
    """Build scenario list based on --htf flag.

    htf_mode:
      "none"  → only base (no filter)
      "4h"    → only +4h filter variants
      "4h1d"  → only +4h+1d filter variants
      "all"   → all 3 variants of every base scenario
    """
    variants: list[tuple[str, list[str]]]
    if htf_mode == "none":
        variants = [("", [])]
    elif htf_mode == "4h":
        variants = [("-4h", ["4h"])]
    elif htf_mode == "4h1d":
        variants = [("-4h1d", ["4h", "1d"])]
    else:  # all
        variants = _HTF_VARIANTS

    result = []
    for (base, tf, vol_z, lb, rr, sl, pos) in _BASE_PARAMS:
        for (suffix, htf) in variants:
            result.append(Scenario(
                name=base + suffix,
                timeframe=tf,
                vol_z=vol_z,
                lookback=lb,
                tp_rr=rr,
                sl_mult=sl,
                max_pos=pos,
                htf_filter=htf,
            ))
    return result


def _apply(base_cfg, sc: Scenario):
    cfg = copy.deepcopy(base_cfg)
    cfg.strategy.timeframe = sc.timeframe
    cfg.strategy.volume_zscore_min = sc.vol_z
    cfg.strategy.breakout_lookback_candles = sc.lookback
    cfg.risk.tp_rr = sc.tp_rr
    cfg.risk.sl_atr_multiplier = sc.sl_mult
    cfg.risk.max_concurrent_positions = sc.max_pos
    return cfg


def _summarise(result: BtResult) -> dict[str, Any]:
    trades = [t for t in result.trades if t.exit_reason != ""]
    if not trades:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "pnl_pct": 0.0,
                "pf": 0.0, "dd": 0.0, "avg_w": 0.0, "avg_l": 0.0, "avg_hold": 0.0}

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
        "wr": round(len(wins) / len(trades) * 100, 2),
        "pnl": round(total_pnl, 2),
        "pnl_pct": round(total_pnl / result.initial_equity * 100, 2),
        "pf": round(pf, 3),
        "dd": round(max_dd, 2),
        "avg_w": round(mean(t.pnl_usd for t in wins) if wins else 0.0, 2),
        "avg_l": round(mean(t.pnl_usd for t in losses) if losses else 0.0, 2),
        "avg_hold": round(mean(t.hold_h for t in trades), 2),
    }


def _write_audit_log(records: list[dict], coins: list[str], days: int, out_dir: str = "data") -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"sweep_{ts}.json")
    with open(path, "w") as f:
        json.dump({
            "run_at": datetime.now(tz=timezone.utc).isoformat(),
            "coins": coins,
            "days": days,
            "scenarios": records,
        }, f, indent=2)
    return path


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m src.backtest.scenarios")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--coins", nargs="+", default=None)
    p.add_argument("--htf", default="all",
                   choices=["none", "4h", "4h1d", "all"],
                   help="HTF trend filter mode (default: all)")
    p.add_argument("--names", nargs="+", default=None,
                   help="Run only scenarios whose name matches one of these strings")
    args = p.parse_args()

    base_cfg = load_config()
    secrets = load_secrets()
    coins = args.coins or base_cfg.data.coins

    scenarios = _build_scenarios(args.htf)
    if args.names:
        scenarios = [s for s in scenarios if any(n in s.name for n in args.names)]
        if not scenarios:
            print(f"No scenarios matched --names {args.names}")
            return

    init_db(secrets.database_url)
    sf = make_session_factory(secrets.database_url)

    # ── Load data once per primary timeframe ───────────────────────────────────
    primary_tfs = list(dict.fromkeys(sc.timeframe for sc in scenarios))
    htf_tfs_needed = list(dict.fromkeys(
        tf for sc in scenarios for tf in sc.htf_filter
    ))
    all_tfs_to_load = list(dict.fromkeys(primary_tfs + htf_tfs_needed))

    print(f"\nLoading data: {all_tfs_to_load} ...")
    caches: dict[str, CandleCache] = {}
    for tf in all_tfs_to_load:
        t0 = time.time()
        caches[tf] = load_candle_cache(sf, coins, tf, args.days)
        btc_n = len(caches[tf].candles.get("BTC", []))
        print(f"  {tf}: {btc_n} BTC candles in {time.time()-t0:.1f}s")

    print(f"\nRunning {len(scenarios)} scenarios on {', '.join(coins)} | {args.days}d\n")

    records: list[dict] = []
    for sc in scenarios:
        cfg = _apply(base_cfg, sc)
        htf_caches = {tf: caches[tf] for tf in sc.htf_filter if tf in caches}
        t0 = time.time()
        result = run(
            sf, coins, sc.timeframe, args.days, cfg,
            cache=caches[sc.timeframe],
            htf_caches=htf_caches,
            htf_filter=sc.htf_filter,
        )
        elapsed = time.time() - t0
        stats = _summarise(result)

        htf_tag = f"htf={'|'.join(sc.htf_filter) or '-':>6}"
        sign = "+" if stats["pnl"] >= 0 else ""
        print(
            f"  {sc.name:<26}  tf={sc.timeframe:<3}  {htf_tag}  "
            f"z={sc.vol_z}  lb={sc.lookback:>2}  rr={sc.tp_rr}  "
            f"→  N={stats['n']:>4}  WR={stats['wr']:>5.1f}%  "
            f"PnL={sign}{stats['pnl']:>8,.0f}  PF={stats['pf']:>5.3f}  "
            f"DD={stats['dd']:>5.1f}%  [{elapsed:.1f}s]"
        )

        records.append({
            "scenario": sc.name,
            "params": {
                "timeframe": sc.timeframe, "vol_z": sc.vol_z,
                "lookback": sc.lookback, "tp_rr": sc.tp_rr,
                "sl_mult": sc.sl_mult, "max_pos": sc.max_pos,
                "htf_filter": sc.htf_filter,
            },
            "stats": stats,
            "elapsed_s": round(elapsed, 1),
        })

    # ── Ranked table ──────────────────────────────────────────────────────────
    print(f"\n{'═'*116}")
    print(f"  RANKING  (by Profit Factor ↓)\n{'═'*116}")
    print(
        f"  {'#':>2}  {'Scenario':<26}  {'tf':<3}  {'HTF':<6}  "
        f"{'N':>5}  {'WR%':>6}  {'PnL$':>10}  {'PnL%':>7}  "
        f"{'PF':>6}  {'MaxDD%':>7}  {'AvgW':>7}  {'AvgL':>7}  {'AvgH':>5}"
    )
    print(f"  {'─'*112}")

    ranked = sorted(records, key=lambda r: r["stats"]["pf"], reverse=True)
    for i, rec in enumerate(ranked, 1):
        s = rec["stats"]
        p = rec["params"]
        sign = "+" if s["pnl"] >= 0 else ""
        htf_tag = "|".join(p["htf_filter"]) or "-"
        print(
            f"  {i:>2}  {rec['scenario']:<26}  {p['timeframe']:<3}  {htf_tag:<6}  "
            f"{s['n']:>5}  {s['wr']:>6.1f}%  "
            f"{sign}{s['pnl']:>9,.0f}  {sign}{s['pnl_pct']:>6.1f}%  "
            f"{s['pf']:>6.3f}  {s['dd']:>6.1f}%  "
            f"{s['avg_w']:>+7.1f}  {s['avg_l']:>+7.1f}  {s['avg_hold']:>5.1f}h"
        )

    print(f"{'═'*116}\n")

    best = ranked[0]
    bs, bp = best["stats"], best["params"]
    sign = "+" if bs["pnl"] >= 0 else ""
    print(f"  Best: {best['scenario']}")
    print(f"    tf={bp['timeframe']}  htf={bp['htf_filter']}  vol_z={bp['vol_z']}  lookback={bp['lookback']}")
    print(f"    tp_rr={bp['tp_rr']}  sl_mult={bp['sl_mult']}  max_pos={bp['max_pos']}")
    print(f"    PnL={sign}{bs['pnl']:,.2f} ({sign}{bs['pnl_pct']:.2f}%)  "
          f"WR={bs['wr']:.1f}%  PF={bs['pf']:.3f}  DD={bs['dd']:.1f}%\n")

    log_path = _write_audit_log(records, coins, args.days)
    print(f"  Audit log → {log_path}\n")


if __name__ == "__main__":
    main()
