"""
Scenario sweep — runs multiple param combinations through the backtest and
prints a ranked comparison table plus writes a JSON audit log.

CLI:
    python -m src.backtest.scenarios --days 90 --htf match
    python -m src.backtest.scenarios --names S07 --wf 3 --slippage 5 20 50
    python -m src.backtest.scenarios --names S07-1h-filter --htf match \\
        --grid-vol-z 2.0 2.2 2.5 --grid-tp-rr 2.5 2.75 3.0 \\
        --rolling-universe 10 --wf 3 --slippage 5 20 50 --days 180
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
    ema_spread_min_pct: float = 0.0
    atr_expansion_min: float = 0.0
    min_vol_24h_usd: float = 0.0
    confirm_bars: int = 1


# ── base scenario definitions ──────────────────────────────────────────────────

_BASE_PARAMS = [
    # (name,             tf,    vol_z, lb, rr,  sl,  pos)
    # ── 15m strategies ────────────────────────────────
    ("S00-baseline",    "15m", 1.5,  20, 2.0, 1.5, 3),
    ("S01-filter-med",  "15m", 2.0,  30, 2.0, 1.5, 3),
    ("S02-filter-high", "15m", 2.5,  40, 2.0, 1.5, 2),
    ("S03-rr-3x",       "15m", 1.5,  20, 3.0, 1.5, 3),
    ("S04-filter+rr",   "15m", 2.0,  30, 3.0, 1.5, 2),
    ("S05-tight-sl",    "15m", 2.0,  30, 2.0, 1.0, 3),
    ("S09-serial",      "15m", 2.0,  30, 2.0, 1.5, 1),
    # ── 1h strategies ─────────────────────────────────
    ("S06-1h-base",     "1h",  1.5,  20, 2.0, 1.5, 3),
    ("S07-1h-filter",   "1h",  2.0,  30, 2.5, 1.5, 2),
    ("S08-1h-highconv", "1h",  2.5,  40, 3.0, 1.5, 2),
    # ── 4h strategies ─────────────────────────────────
    ("S10-4h-base",     "4h",  1.5,  20, 2.0, 1.5, 2),
    ("S11-4h-filter",   "4h",  2.0,  30, 2.5, 1.5, 2),
    ("S12-4h-highconv", "4h",  2.5,  40, 3.0, 1.5, 1),
]

_TF_HTF_MATCH: dict[str, list[str]] = {
    "15m": ["1h"],
    "1h":  ["4h"],
    "4h":  ["1d"],
}

_TF_HTF_ALL: dict[str, list[tuple[str, list[str]]]] = {
    "15m": [("",      []),
            ("-1h",   ["1h"]),
            ("-4h",   ["4h"]),
            ("-4h1d", ["4h", "1d"])],
    "1h":  [("",    []),
            ("-4h", ["4h"])],
    "4h":  [("",    []),
            ("-1d", ["1d"])],
}


def _build_scenarios(htf_mode: str) -> list[Scenario]:
    result = []
    for (base, tf, vol_z, lb, rr, sl, pos) in _BASE_PARAMS:
        if htf_mode == "none":
            variants: list[tuple[str, list[str]]] = [("", [])]
        elif htf_mode == "match":
            htf = _TF_HTF_MATCH.get(tf, [])
            suffix = "-" + "".join(htf) if htf else ""
            variants = [(suffix, htf)]
        else:  # all
            variants = _TF_HTF_ALL.get(tf, [("", [])])

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


def _expand_grid(
    base_scenarios: list[Scenario],
    vol_zs: list[float],
    tp_rrs: list[float],
    ema_spreads: list[float] | None = None,
    atr_expansions: list[float] | None = None,
    min_vols: list[float] | None = None,
    confirm_bars_list: list[int] | None = None,
) -> list[Scenario]:
    """Cartesian product of all grid dimensions over base_scenarios."""
    result = []
    _ema_spreads = ema_spreads or [0.0]
    _atr_expansions = atr_expansions or [0.0]
    _min_vols = min_vols or [0.0]
    _confirm_bars = confirm_bars_list or [1]
    for sc in base_scenarios:
        htf_sfx = ("-" + "".join(sc.htf_filter)) if sc.htf_filter else ""
        base_name = sc.name
        if htf_sfx and base_name.endswith(htf_sfx):
            base_name = base_name[: -len(htf_sfx)]
        for vz in vol_zs:
            for rr in tp_rrs:
                for sp in _ema_spreads:
                    for ae in _atr_expansions:
                        for mv in _min_vols:
                            for cb in _confirm_bars:
                                parts = [f"{base_name}-z{vz:.1f}-rr{rr:.2f}"]
                                if sp > 0:
                                    parts.append(f"sp{sp:.2f}")
                                if ae > 0:
                                    parts.append(f"atr{ae:.1f}")
                                if mv > 0:
                                    parts.append(f"v{int(mv//1_000_000)}m")
                                if cb > 1:
                                    parts.append(f"c{cb}")
                                name = "-".join(parts) + htf_sfx
                                result.append(Scenario(
                                    name=name,
                                    timeframe=sc.timeframe,
                                    vol_z=vz,
                                    lookback=sc.lookback,
                                    tp_rr=rr,
                                    sl_mult=sc.sl_mult,
                                    max_pos=sc.max_pos,
                                    htf_filter=list(sc.htf_filter),
                                    ema_spread_min_pct=sp,
                                    atr_expansion_min=ae,
                                    min_vol_24h_usd=mv,
                                    confirm_bars=cb,
                                ))
    return result


def _apply(base_cfg, sc: Scenario):
    cfg = copy.deepcopy(base_cfg)
    cfg.strategy.timeframe = sc.timeframe
    cfg.strategy.volume_zscore_min = sc.vol_z
    cfg.strategy.breakout_lookback_candles = sc.lookback
    cfg.strategy.ema_spread_min_pct = sc.ema_spread_min_pct
    cfg.strategy.atr_expansion_min = sc.atr_expansion_min
    cfg.strategy.min_vol_24h_usd = sc.min_vol_24h_usd
    cfg.strategy.breakout_confirm_bars = sc.confirm_bars
    cfg.risk.tp_rr = sc.tp_rr
    cfg.risk.sl_atr_multiplier = sc.sl_mult
    cfg.risk.max_concurrent_positions = sc.max_pos
    return cfg


def _summarise(result: BtResult) -> dict[str, Any]:
    trades = [t for t in result.trades if t.exit_reason != ""]
    if not trades:
        return {
            "n": 0, "wr": 0.0, "pnl": 0.0, "pnl_pct": 0.0,
            "pf": 0.0, "dd": 0.0, "avg_w": 0.0, "avg_l": 0.0,
            "avg_hold": 0.0, "per_coin": {},
        }

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

    per_coin: dict[str, dict] = {}
    for coin in list(dict.fromkeys(t.coin for t in trades)):
        ct = [t for t in trades if t.coin == coin]
        cw = [t for t in ct if t.pnl_usd > 0]
        cl = [t for t in ct if t.pnl_usd <= 0]
        c_pnl = sum(t.pnl_usd for t in ct)
        c_gw = sum(t.pnl_usd for t in cw)
        c_gl = abs(sum(t.pnl_usd for t in cl))
        per_coin[coin] = {
            "n": len(ct),
            "wr": round(len(cw) / len(ct) * 100, 1),
            "pnl": round(c_pnl, 1),
            "pf": round(c_gw / c_gl, 3) if c_gl else math.inf,
        }

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
        "per_coin": per_coin,
    }


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b%d")


def _write_audit_log(
    records: list[dict],
    coins: list[str],
    days: int,
    args_dict: dict,
    out_dir: str = "data",
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"sweep_{ts}.json")
    with open(path, "w") as f:
        json.dump(
            {
                "run_at": datetime.now(tz=timezone.utc).isoformat(),
                "coins": coins,
                "days": days,
                "args": args_dict,
                "scenarios": records,
            },
            f,
            indent=2,
            default=lambda x: None if x == math.inf else x,
        )
    return path


def _coin_earliest(cache: CandleCache, delay_days: int, no_delay: set[str] | None = None) -> dict[str, int]:
    forced = no_delay or {"BTC"}
    result: dict[str, int] = {}
    for coin, candles in cache.candles.items():
        if coin in forced or not candles:
            result[coin] = 0
        else:
            result[coin] = candles[0].open_time + delay_days * 86400
    return result


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m src.backtest.scenarios")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--coins", nargs="+", default=None)
    p.add_argument("--htf", default="match", choices=["none", "match", "all"])
    p.add_argument("--names", nargs="+", default=None,
                   help="Run only scenarios whose name contains one of these strings")
    p.add_argument("--wf", type=int, default=None, metavar="N",
                   help="Walk-forward: split date range into N equal windows")
    p.add_argument("--slippage", nargs="+", type=float, default=None, metavar="BPS",
                   help="Slippage stress test: list of bps values (e.g. 5 20 50)")
    p.add_argument("--delay", nargs="+", type=int, default=None, metavar="DAYS")
    p.add_argument("--rolling-universe", type=int, default=None, metavar="N",
                   help="Only trade top-N coins by 24h vol at each candle")
    p.add_argument("--grid-vol-z", nargs="+", type=float, default=None, metavar="Z",
                   help="Grid: list of vol_z values (e.g. 2.0 2.2 2.5)")
    p.add_argument("--grid-tp-rr", nargs="+", type=float, default=None, metavar="RR",
                   help="Grid: list of tp_rr values (e.g. 2.5 2.75 3.0)")
    p.add_argument("--grid-ema-spread", nargs="+", type=float, default=None, metavar="PCT",
                   help="Grid: EMA20/50 spread filter values in %% (e.g. 0.0 0.3 0.5 0.8)")
    p.add_argument("--grid-atr-expansion", nargs="+", type=float, default=None, metavar="MULT",
                   help="Grid: ATR expansion multiplier vs mean ATR (e.g. 0.0 1.0 1.1 1.2)")
    p.add_argument("--grid-min-vol", nargs="+", type=float, default=None, metavar="USD",
                   help="Grid: min 24h dollar volume per coin (e.g. 0 10e6 50e6 100e6)")
    p.add_argument("--grid-confirm", nargs="+", type=int, default=None, metavar="N",
                   help="Grid: breakout confirmation bars (1=immediate, 2=next-bar confirm)")
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

    # Expand grid if requested
    if any([args.grid_vol_z, args.grid_tp_rr, args.grid_ema_spread,
             args.grid_atr_expansion, args.grid_min_vol, args.grid_confirm]):
        vol_zs = args.grid_vol_z or [s.vol_z for s in scenarios]
        tp_rrs = args.grid_tp_rr or [s.tp_rr for s in scenarios]
        scenarios = _expand_grid(
            scenarios, vol_zs, tp_rrs,
            ema_spreads=args.grid_ema_spread,
            atr_expansions=args.grid_atr_expansion,
            min_vols=args.grid_min_vol,
            confirm_bars_list=args.grid_confirm,
        )

    init_db(secrets.database_url)
    sf = make_session_factory(secrets.database_url)

    # Load data once per primary timeframe
    primary_tfs = list(dict.fromkeys(sc.timeframe for sc in scenarios))
    htf_tfs_needed = list(dict.fromkeys(tf for sc in scenarios for tf in sc.htf_filter))
    all_tfs_to_load = list(dict.fromkeys(primary_tfs + htf_tfs_needed))
    need_vol_cache = args.rolling_universe or (args.grid_min_vol and any(v > 0 for v in args.grid_min_vol))
    if need_vol_cache and "1h" not in all_tfs_to_load:
        all_tfs_to_load.append("1h")

    print(f"\nLoading data: {all_tfs_to_load} ...")
    caches: dict[str, CandleCache] = {}
    for tf in all_tfs_to_load:
        t0 = time.time()
        caches[tf] = load_candle_cache(sf, coins, tf, args.days)
        btc_n = len(caches[tf].candles.get("BTC", []))
        print(f"  {tf}: {btc_n} BTC candles in {time.time()-t0:.1f}s")

    vol_cache = caches.get("1h") if need_vol_cache else None

    print(f"\nRunning {len(scenarios)} scenarios on {', '.join(coins)} | {args.days}d\n")

    # ── Main scenario loop ────────────────────────────────────────────────────
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
            rolling_universe_n=args.rolling_universe,
            vol_cache=vol_cache,
        )
        elapsed = time.time() - t0
        stats = _summarise(result)

        htf_tag = f"htf={'|'.join(sc.htf_filter) or '-':>6}"
        sign = "+" if stats["pnl"] >= 0 else ""
        print(
            f"  {sc.name:<30}  tf={sc.timeframe:<3}  {htf_tag}  "
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
                "ema_spread_min_pct": sc.ema_spread_min_pct,
                "atr_expansion_min": sc.atr_expansion_min,
                "min_vol_24h_usd": sc.min_vol_24h_usd,
                "confirm_bars": sc.confirm_bars,
                "rolling_universe_n": args.rolling_universe,
            },
            "overall": {k: v for k, v in stats.items() if k != "per_coin"},
            "per_coin": stats["per_coin"],
            "walk_forward": [],
            "slippage_stress": [],
            "elapsed_s": round(elapsed, 1),
        })

    # ── Ranked table ─────────────────────────────────────────────────────────
    W = 120
    print(f"\n{'═'*W}")
    print(f"  RANKING  (by Profit Factor ↓)\n{'═'*W}")
    print(
        f"  {'#':>2}  {'Scenario':<30}  {'tf':<3}  {'HTF':<6}  "
        f"{'N':>5}  {'WR%':>6}  {'PnL$':>10}  {'PnL%':>7}  "
        f"{'PF':>6}  {'MaxDD%':>7}  {'AvgW':>7}  {'AvgL':>7}  {'AvgH':>5}"
    )
    print(f"  {'─'*116}")

    ranked = sorted(records, key=lambda r: r["overall"]["pf"], reverse=True)
    for i, rec in enumerate(ranked, 1):
        s = rec["overall"]
        p = rec["params"]
        sign = "+" if s["pnl"] >= 0 else ""
        htf_tag = "|".join(p["htf_filter"]) or "-"
        pf_s = f"{s['pf']:.3f}" if s["pf"] != math.inf else "  inf"
        print(
            f"  {i:>2}  {rec['scenario']:<30}  {p['timeframe']:<3}  {htf_tag:<6}  "
            f"{s['n']:>5}  {s['wr']:>6.1f}%  "
            f"{sign}{s['pnl']:>9,.0f}  {sign}{s['pnl_pct']:>6.1f}%  "
            f"{pf_s:>6}  {s['dd']:>6.1f}%  "
            f"{s['avg_w']:>+7.1f}  {s['avg_l']:>+7.1f}  {s['avg_hold']:>5.1f}h"
        )

    print(f"{'═'*W}\n")

    # Per-coin breakdown (best scenario)
    best = ranked[0]
    bs, bp = best["overall"], best["params"]
    sign = "+" if bs["pnl"] >= 0 else ""
    print(f"  Best: {best['scenario']}")
    print(f"    tf={bp['timeframe']}  htf={bp['htf_filter']}  vol_z={bp['vol_z']}  lookback={bp['lookback']}")
    print(f"    tp_rr={bp['tp_rr']}  sl_mult={bp['sl_mult']}  max_pos={bp['max_pos']}")
    pf_s = f"{bs['pf']:.3f}" if bs["pf"] != math.inf else "inf"
    print(f"    PnL={sign}{bs['pnl']:,.2f} ({sign}{bs['pnl_pct']:.2f}%)  "
          f"WR={bs['wr']:.1f}%  PF={pf_s}  DD={bs['dd']:.1f}%\n")

    pc = best.get("per_coin", {})
    if pc:
        print(f"\n{'─'*70}")
        print(f"  PER-COIN BREAKDOWN: {best['scenario']}")
        print(f"  {'Coin':<6}  {'N':>5}  {'WR%':>6}  {'PnL$':>9}  {'PF':>6}")
        print(f"  {'─'*42}")
        for coin, cs in sorted(pc.items(), key=lambda x: x[1]["pnl"], reverse=True):
            sign = "+" if cs["pnl"] >= 0 else ""
            pf_s = f"{cs['pf']:.3f}" if cs["pf"] != math.inf else "  inf"
            print(f"  {coin:<6}  {cs['n']:>5}  {cs['wr']:>6.1f}%  {sign}{cs['pnl']:>8,.0f}  {pf_s:>6}")
        print()

    # ── Walk-forward ──────────────────────────────────────────────────────────
    if args.wf:
        n_windows = args.wf
        btc_candles_all = caches[primary_tfs[0]].candles.get("BTC", [])
        if not btc_candles_all:
            print("  Walk-forward: no BTC candles in cache, skipping.")
        else:
            full_since = btc_candles_all[0].open_time
            full_until = btc_candles_all[-1].close_time
            window_secs = (full_until - full_since) // n_windows
            window_days = window_secs // 86400

            print(f"\n{'═'*W}")
            print(f"  WALK-FORWARD: {n_windows} windows × ~{window_days}d  "
                  f"({_fmt_date(full_since)} → {_fmt_date(full_until)})")
            print(f"{'═'*W}")

            for rec in ranked:
                sc_name = rec["scenario"]
                sc = next(s for s in scenarios if s.name == sc_name)
                cfg = _apply(base_cfg, sc)
                htf_caches = {tf: caches[tf] for tf in sc.htf_filter if tf in caches}

                print(f"\n  {sc_name}:")
                wf_windows: list[dict] = []
                for w in range(n_windows):
                    win_since = full_since + w * window_secs
                    win_until = (full_since + (w + 1) * window_secs
                                 if w < n_windows - 1 else full_until)

                    res = run(
                        sf, coins, sc.timeframe, args.days, cfg,
                        cache=caches[sc.timeframe],
                        htf_caches=htf_caches,
                        htf_filter=sc.htf_filter,
                        since_ts=win_since,
                        until_ts=win_until,
                        rolling_universe_n=args.rolling_universe,
                        vol_cache=vol_cache,
                    )
                    ws = _summarise(res)
                    pf_s = f"{ws['pf']:.3f}" if ws["pf"] != math.inf else "  inf"
                    sign = "+" if ws["pnl"] >= 0 else ""
                    print(
                        f"    W{w+1} {_fmt_date(win_since)}→{_fmt_date(win_until)}:"
                        f"  N={ws['n']:>4}  WR={ws['wr']:>5.1f}%"
                        f"  PnL={sign}{ws['pnl']:>8,.0f}"
                        f"  PF={pf_s}  DD={ws['dd']:>4.1f}%"
                    )
                    wf_windows.append({
                        "window": w + 1,
                        "since": _fmt_ts(win_since),
                        "until": _fmt_ts(win_until),
                        **{k: v for k, v in ws.items() if k != "per_coin"},
                    })

                n_profit = sum(1 for w in wf_windows if w["pnl"] > 0)
                valid_pfs = [w["pf"] for w in wf_windows if w["pf"] not in (math.inf, 0.0)]
                avg_pf = round(mean(valid_pfs), 3) if valid_pfs else 0.0
                worst_pf = round(min(valid_pfs), 3) if valid_pfs else 0.0
                print(f"    → {n_profit}/{n_windows} profitable  avg_PF={avg_pf:.3f}  worst_PF={worst_pf:.3f}")

                rec["walk_forward"] = wf_windows
                rec["n_profitable_windows"] = n_profit
                rec["avg_window_pf"] = avg_pf
                rec["worst_window_pf"] = worst_pf

    # ── Slippage stress ───────────────────────────────────────────────────────
    if args.slippage:
        print(f"\n{'═'*W}")
        print(f"  SLIPPAGE STRESS  (bps: {args.slippage})")
        print(f"{'═'*W}")

        for rec in ranked:
            sc_name = rec["scenario"]
            sc = next(s for s in scenarios if s.name == sc_name)
            cfg = _apply(base_cfg, sc)
            htf_caches = {tf: caches[tf] for tf in sc.htf_filter if tf in caches}

            print(f"\n  {sc_name}:")
            slip_results: list[dict] = []
            base_pnl: float | None = None
            for bps in args.slippage:
                res = run(
                    sf, coins, sc.timeframe, args.days, cfg,
                    cache=caches[sc.timeframe],
                    htf_caches=htf_caches,
                    htf_filter=sc.htf_filter,
                    slippage_bps=bps,
                    rolling_universe_n=args.rolling_universe,
                    vol_cache=vol_cache,
                )
                ss = _summarise(res)
                sign = "+" if ss["pnl"] >= 0 else ""
                if base_pnl is None:
                    base_pnl = ss["pnl"]
                    chg = "  (base)"
                else:
                    pct = (ss["pnl"] - base_pnl) / abs(base_pnl) * 100 if base_pnl else 0
                    chg = f"  ({pct:+.1f}%)"
                pf_s = f"{ss['pf']:.3f}" if ss["pf"] != math.inf else "inf"
                print(
                    f"    {bps:>5.0f}bps:  N={ss['n']:>4}  WR={ss['wr']:>5.1f}%"
                    f"  PnL={sign}{ss['pnl']:>8,.0f}  PF={pf_s}{chg}"
                )
                slip_results.append({
                    "bps": bps,
                    **{k: v for k, v in ss.items() if k != "per_coin"},
                })

            rec["slippage_stress"] = slip_results

    # ── Delayed inclusion ─────────────────────────────────────────────────────
    if args.delay:
        print(f"\n{'═'*W}")
        print(f"  DELAYED INCLUSION  (BTC always included; others delayed from first candle)")
        print(f"{'═'*W}")

        for rec in ranked:
            sc_name = rec["scenario"]
            sc = next(s for s in scenarios if s.name == sc_name)
            cfg = _apply(base_cfg, sc)
            htf_caches = {tf: caches[tf] for tf in sc.htf_filter if tf in caches}
            base_pnl = rec["overall"]["pnl"]

            print(f"\n  {sc_name}:")
            for coin, candles in sorted(caches[sc.timeframe].candles.items()):
                if coin != "BTC" and coin in coins and candles:
                    print(f"    {coin:<6} data starts {_fmt_date(candles[0].open_time)}")

            sign0 = "+" if base_pnl >= 0 else ""
            print(
                f"\n    {'0d (base)':<12}  N={rec['overall']['n']:>4}  WR={rec['overall']['wr']:>5.1f}%"
                f"  PnL={sign0}{base_pnl:>8,.0f}  PF={rec['overall']['pf']:>5.3f}  (base)"
            )

            delay_results: list[dict] = []
            for d in args.delay:
                earliest = _coin_earliest(caches[sc.timeframe], d)
                res = run(
                    sf, coins, sc.timeframe, args.days, cfg,
                    cache=caches[sc.timeframe],
                    htf_caches=htf_caches,
                    htf_filter=sc.htf_filter,
                    coin_earliest=earliest,
                )
                ds = _summarise(res)
                sign = "+" if ds["pnl"] >= 0 else ""
                pct = (ds["pnl"] - base_pnl) / abs(base_pnl) * 100 if base_pnl else 0
                print(
                    f"    {f'{d}d delay':<12}  N={ds['n']:>4}  WR={ds['wr']:>5.1f}%"
                    f"  PnL={sign}{ds['pnl']:>8,.0f}  PF={ds['pf']:>5.3f}  ({pct:+.1f}%)"
                )
                delay_results.append({
                    "delay_days": d,
                    **{k: v for k, v in ds.items() if k != "per_coin"},
                })

            rec["delay_stress"] = delay_results

    # ── Audit log ────────────────────────────────────────────────────────────
    args_dict = {
        "days": args.days,
        "coins": coins,
        "htf": args.htf,
        "names": args.names,
        "wf": args.wf,
        "slippage": args.slippage,
        "delay": args.delay,
        "rolling_universe": args.rolling_universe,
        "grid_vol_z": args.grid_vol_z,
        "grid_tp_rr": args.grid_tp_rr,
        "grid_ema_spread": args.grid_ema_spread,
        "grid_atr_expansion": args.grid_atr_expansion,
        "grid_min_vol": args.grid_min_vol,
        "grid_confirm": args.grid_confirm,
    }
    log_path = _write_audit_log(records, coins, args.days, args_dict)
    print(f"\n  Audit log → {log_path}\n")


if __name__ == "__main__":
    main()
