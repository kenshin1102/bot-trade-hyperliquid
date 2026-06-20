"""
Backtest runner — replays DB candles through Breakout_V1 + regime pipeline.

OI change is not available historically so the OI gate is bypassed automatically.
Funding rate history and candle OHLCV are used as-is from the database.

CLI:
    python -m src.backtest.runner
    python -m src.backtest.runner --coins BTC ETH --days 60
    python -m src.backtest.runner --coins BTC --timeframe 15m --days 90
"""
from __future__ import annotations

import argparse
import bisect
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, stdev

from src.config.settings import load_config, load_secrets
from src.storage.db import CandleRow, FundingRateRow, init_db, make_session_factory
from src.strategy.breakout import BreakoutV1
from src.strategy.breakout_v2 import BreakoutV2
from src.strategy.feature_engine import Features, _atr, _ema
from src.strategy.regime import RegimeDetector

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("backtest")

_MIN_CANDLES = 55   # EMA50 (50) + ATR (14) warmup, cộng thêm buffer
_MAX_FUNDING_WINDOW = 90  # funding rows used for percentile calc


# ── data structures ────────────────────────────────────────────────────────────

@dataclass
class BtTrade:
    coin: str
    side: str
    entry_time: int
    entry_price: float
    sl_price: float
    tp_price: float
    size_notional: float
    fee_open: float
    regime_score: float
    regime: str
    exit_time: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_usd: float = 0.0
    fee_close: float = 0.0

    @property
    def pnl_pct(self) -> float:
        return self.pnl_usd / self.size_notional * 100 if self.size_notional else 0.0

    @property
    def hold_h(self) -> float:
        return (self.exit_time - self.entry_time) / 3600


@dataclass
class BtResult:
    trades: list[BtTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    initial_equity: float = 10000.0


@dataclass
class CandleCache:
    """Pre-loaded candle + funding data for one timeframe, reusable across scenarios."""
    candles: dict[str, list[CandleRow]]
    funding: dict[str, list[FundingRateRow]]
    candle_ts: dict[str, list[int]]   # close_time arrays for bisect
    funding_ts: dict[str, list[int]]  # funding_time arrays for bisect


_HTF_EMA_FAST = 20
_HTF_EMA_SLOW = 50
_VOL_WINDOW_CANDLES = 24  # 24 × 1h = rolling 24h dollar volume


def _rolling_vol_24h(coin: str, cache: CandleCache, ts: int) -> float:
    """Sum of (close × volume) for the last 24 candles up to ts."""
    ts_arr = cache.candle_ts.get(coin, [])
    candles = cache.candles.get(coin, [])
    if not ts_arr or not candles:
        return 0.0
    idx = bisect.bisect_right(ts_arr, ts) - 1
    if idx < 0:
        return 0.0
    start = max(0, idx - _VOL_WINDOW_CANDLES + 1)
    return sum(c.close * c.volume for c in candles[start:idx + 1])


def _top_coins_by_vol(coins: list[str], cache: CandleCache, ts: int, n: int) -> list[str]:
    """Return top-n coins ranked by rolling 24h dollar volume at ts."""
    ranked = sorted(
        coins,
        key=lambda c: _rolling_vol_24h(c, cache, ts),
        reverse=True,
    )
    return ranked[:n]


def _htf_trend_ok(
    side: str,
    coin: str,
    ts: int,
    htf_caches: dict[str, CandleCache],
    tfs: list[str],
) -> bool:
    """Return True if every requested HTF EMA trend agrees with `side`.

    Bullish  = EMA20 > EMA50 on that timeframe → allow LONG
    Bearish  = EMA20 < EMA50                   → allow SHORT
    Missing data → don't block (return True).
    """
    for tf in tfs:
        cache = htf_caches.get(tf)
        if cache is None:
            continue
        ts_arr = cache.candle_ts.get(coin, [])
        if not ts_arr:
            continue
        idx = bisect.bisect_right(ts_arr, ts) - 1
        if idx < _HTF_EMA_SLOW - 1:
            continue  # not enough history yet
        window = cache.candles[coin][idx - _HTF_EMA_SLOW + 1: idx + 1]
        closes = [c.close for c in window]
        ema_fast = _ema(closes[-_HTF_EMA_FAST:], _HTF_EMA_FAST)
        ema_slow = _ema(closes, _HTF_EMA_SLOW)
        htf_bull = ema_fast > ema_slow
        if side == "LONG" and not htf_bull:
            return False
        if side == "SHORT" and htf_bull:
            return False
    return True


# ── feature computation (standalone, no DB calls) ─────────────────────────────

def _compute_features(
    coin: str,
    timeframe: str,
    candles: list[CandleRow],
    funding_rows: list[FundingRateRow],
) -> Features | None:
    if len(candles) < _MIN_CANDLES:
        return None

    closes = [c.close for c in candles]
    ema_20 = _ema(closes[-20:], 20)
    ema_50 = _ema(closes[-50:], 50)
    atr = _atr(candles[-15:])
    atr_mean = mean(c.high - c.low for c in candles[-30:])

    vols = [c.volume for c in candles[-20:]]
    mean_v = mean(vols[:-1])
    std_v = stdev(vols[:-1]) if len(vols) > 2 else 0.0
    volume_zscore = (vols[-1] - mean_v) / std_v if std_v > 0 else 0.0

    funding_rate = funding_rows[-1].rate if funding_rows else 0.0
    if len(funding_rows) >= 2:
        rates = [r.rate for r in funding_rows]
        funding_percentile = sum(1 for r in rates if r < funding_rate) / len(rates)
    else:
        funding_percentile = 0.5

    return Features(
        coin=coin,
        timeframe=timeframe,
        feature_time=candles[-1].close_time,
        ema_20=ema_20,
        ema_50=ema_50,
        atr=atr,
        atr_mean=atr_mean,
        volume_zscore=volume_zscore,
        oi_change_pct=0.0,   # no historical OI data
        funding_rate=funding_rate,
        funding_percentile=funding_percentile,
    )


# ── SL/TP simulation ──────────────────────────────────────────────────────────

def _check_exit(pos: BtTrade, candle: CandleRow) -> tuple[str, float] | None:
    """Check if SL or TP was hit inside this candle. Returns (reason, price) or None."""
    if pos.side == "LONG":
        sl_hit = candle.low <= pos.sl_price
        tp_hit = candle.high >= pos.tp_price
    else:
        sl_hit = candle.high >= pos.sl_price
        tp_hit = candle.low <= pos.tp_price

    if sl_hit and tp_hit:
        return "sl", pos.sl_price   # conservative: SL first
    if sl_hit:
        return "sl", pos.sl_price
    if tp_hit:
        return "tp", pos.tp_price
    return None


def _close(pos: BtTrade, exit_price: float, exit_time: int, reason: str, fee_bps: float) -> None:
    fee_close = pos.size_notional * fee_bps / 10000
    if pos.side == "LONG":
        gross = pos.size_notional * (exit_price - pos.entry_price) / pos.entry_price
    else:
        gross = pos.size_notional * (pos.entry_price - exit_price) / pos.entry_price
    pos.exit_time = exit_time
    pos.exit_price = exit_price
    pos.exit_reason = reason
    pos.fee_close = fee_close
    pos.pnl_usd = gross - pos.fee_open - fee_close


# ── data loader (call once, reuse across scenarios) ───────────────────────────

def load_candle_cache(
    session_factory,
    coins: list[str],
    timeframe: str,
    days: int,
) -> CandleCache:
    """Load all candles + funding from DB once and return a reusable cache."""
    since_ts = int(time.time()) - days * 86400
    all_candles: dict[str, list[CandleRow]] = {}
    all_funding: dict[str, list[FundingRateRow]] = {}
    all_coins = list(dict.fromkeys(["BTC"] + coins))

    s = session_factory()
    try:
        for coin in all_coins:
            all_candles[coin] = (
                s.query(CandleRow)
                .filter(CandleRow.coin == coin, CandleRow.interval == timeframe,
                        CandleRow.open_time >= since_ts)
                .order_by(CandleRow.open_time)
                .all()
            )
            all_funding[coin] = (
                s.query(FundingRateRow)
                .filter(FundingRateRow.coin == coin, FundingRateRow.funding_time >= since_ts)
                .order_by(FundingRateRow.funding_time)
                .all()
            )
    finally:
        s.close()

    candle_ts = {coin: [c.close_time for c in rows] for coin, rows in all_candles.items()}
    funding_ts = {coin: [f.funding_time for f in rows] for coin, rows in all_funding.items()}
    return CandleCache(candles=all_candles, funding=all_funding,
                       candle_ts=candle_ts, funding_ts=funding_ts)


# ── main runner ───────────────────────────────────────────────────────────────

def run(
    session_factory,
    coins: list[str],
    timeframe: str,
    days: int,
    cfg,
    *,
    cache: CandleCache | None = None,
    htf_caches: dict[str, CandleCache] | None = None,
    htf_filter: list[str] | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
    slippage_bps: float | None = None,
    coin_earliest: dict[str, int] | None = None,
    rolling_universe_n: int | None = None,
    vol_cache: CandleCache | None = None,
) -> BtResult:
    if cache is None:
        cache = load_candle_cache(session_factory, coins, timeframe, days)

    _htf_caches: dict[str, CandleCache] = htf_caches or {}
    _htf_filter: list[str] = htf_filter or []

    all_candles = cache.candles
    all_funding = cache.funding
    candle_ts = cache.candle_ts
    funding_ts = cache.funding_ts

    result = BtResult(initial_equity=cfg.execution.account_balance)
    equity = cfg.execution.account_balance

    # Temporarily disable OI gate (no historical OI available)
    orig_oi_min = cfg.strategy.oi_change_min_pct
    cfg.strategy.oi_change_min_pct = 0.0

    regime_det = RegimeDetector(cfg.regime)
    if cfg.strategy.name == "Breakout_V2":
        strategy = BreakoutV2(cfg.strategy, cfg.risk, regime_det)
        breakout_window = cfg.strategy.breakout_lookback_candles + cfg.strategy.v2_max_bars_to_retest + 2
    else:
        strategy = BreakoutV1(cfg.strategy, cfg.risk, regime_det)
        breakout_window = cfg.strategy.breakout_lookback_candles + 2

    # Drive the loop off BTC candle timestamps
    btc_candles = all_candles.get("BTC", [])
    if len(btc_candles) < _MIN_CANDLES:
        print(f"Not enough BTC candles in DB ({len(btc_candles)} < {_MIN_CANDLES}). Run backfill first.")
        cfg.strategy.oi_change_min_pct = orig_oi_min
        return result

    open_pos: dict[str, BtTrade] = {}
    realized_pnl = 0.0
    today_start = 0
    daily_pnl = 0.0

    for i in range(_MIN_CANDLES, len(btc_candles)):
        btc_c = btc_candles[i]
        ts = btc_c.close_time

        # Walk-forward window filter
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts > until_ts:
            break

        # Reset daily PnL counter at midnight UTC
        day_bucket = ts // 86400
        if day_bucket != today_start:
            today_start = day_bucket
            daily_pnl = 0.0

        # 1. Check SL/TP on all open positions
        for coin in list(open_pos):
            pos = open_pos[coin]
            idx = bisect.bisect_right(candle_ts.get(coin, []), ts) - 1
            if idx < 0:
                continue
            coin_c = all_candles[coin][idx]
            if coin_c.close_time != ts:
                continue
            hit = _check_exit(pos, coin_c)
            if hit:
                reason, exit_price = hit
                _close(pos, exit_price, ts, reason, cfg.execution.fee_taker_bps)
                realized_pnl += pos.pnl_usd
                daily_pnl += pos.pnl_usd
                equity += pos.pnl_usd
                result.trades.append(pos)
                del open_pos[coin]

        # 2. Equity snapshot (realized + unrealized)
        unrealized = 0.0
        for coin, pos in open_pos.items():
            idx = bisect.bisect_right(candle_ts.get(coin, []), ts) - 1
            if idx >= 0:
                price = all_candles[coin][idx].close
                if pos.side == "LONG":
                    unrealized += pos.size_notional * (price - pos.entry_price) / pos.entry_price
                else:
                    unrealized += pos.size_notional * (pos.entry_price - price) / pos.entry_price
        result.equity_curve.append((ts, equity + unrealized))

        # 3. Evaluate signals
        max_daily_loss = cfg.execution.account_balance * cfg.risk.max_daily_loss_pct / 100
        if daily_pnl < -max_daily_loss:
            continue

        # Rolling universe: only trade top-N coins by 24h vol at this timestamp
        if rolling_universe_n is not None:
            _vc = vol_cache if vol_cache is not None else cache
            eligible: set[str] | None = set(_top_coins_by_vol(coins, _vc, ts, rolling_universe_n))
        else:
            eligible = None

        for coin in coins:
            if coin in open_pos:
                continue
            if len(open_pos) >= cfg.risk.max_concurrent_positions:
                break
            # Delayed inclusion: skip coin until its earliest allowed timestamp
            if coin_earliest and ts < coin_earliest.get(coin, 0):
                continue
            # Rolling universe gate
            if eligible is not None and coin not in eligible:
                continue

            # Min 24h dollar volume gate
            if cfg.strategy.min_vol_24h_usd > 0:
                _vc = vol_cache if vol_cache is not None else cache
                if _rolling_vol_24h(coin, _vc, ts) < cfg.strategy.min_vol_24h_usd:
                    continue

            end = bisect.bisect_right(candle_ts.get(coin, []), ts)
            if end < _MIN_CANDLES:
                continue

            # Fixed-size windows — O(1) slice, no O(n) copies
            feat_candles = all_candles[coin][end - _MIN_CANDLES:end]
            strat_candles = all_candles[coin][max(0, end - breakout_window):end]

            end_f = bisect.bisect_right(funding_ts.get(coin, []), ts)
            fund_window = all_funding[coin][max(0, end_f - _MAX_FUNDING_WINDOW):end_f]

            features = _compute_features(coin, timeframe, feat_candles, fund_window)
            if features is None:
                continue

            # BTC features for regime
            end_b = bisect.bisect_right(candle_ts.get("BTC", []), ts)
            btc_feat_candles = all_candles["BTC"][end_b - _MIN_CANDLES:end_b] if end_b >= _MIN_CANDLES else []
            end_bf = bisect.bisect_right(funding_ts.get("BTC", []), ts)
            btc_fund_window = all_funding["BTC"][max(0, end_bf - _MAX_FUNDING_WINDOW):end_bf]
            btc_features = (
                _compute_features("BTC", timeframe, btc_feat_candles, btc_fund_window)
                if coin != "BTC" else None
            )

            current_price = feat_candles[-1].close
            signal = strategy.evaluate(coin, features, btc_features, strat_candles, current_price, 5.0)
            if signal is None:
                continue

            # Higher-timeframe trend filter
            if _htf_filter and not _htf_trend_ok(signal.side, coin, ts, _htf_caches, _htf_filter):
                continue

            _slip = slippage_bps if slippage_bps is not None else cfg.execution.slippage_bps
            adj = (_slip + cfg.execution.fee_taker_bps) / 10000
            fill = current_price * (1 + adj) if signal.side == "LONG" else current_price * (1 - adj)

            sl_dist = abs(fill - signal.sl_price) / fill
            if sl_dist <= 0:
                continue

            risk_usd = cfg.execution.account_balance * cfg.risk.max_risk_per_trade_pct / 100
            size = risk_usd / sl_dist
            fee_open = size * cfg.execution.fee_taker_bps / 10000

            regime_score = regime_det.score(features, btc_features)
            open_pos[coin] = BtTrade(
                coin=coin,
                side=signal.side,
                entry_time=ts,
                entry_price=fill,
                sl_price=signal.sl_price,
                tp_price=signal.tp_price,
                size_notional=size,
                fee_open=fee_open,
                regime_score=regime_score,
                regime=regime_det.classify(regime_score).value,
            )

    # Close remaining positions at last available price
    for coin, pos in open_pos.items():
        rows = all_candles.get(coin, [])
        if rows:
            _close(pos, rows[-1].close, rows[-1].close_time, "end_of_data",
                   cfg.execution.fee_taker_bps)
        result.trades.append(pos)

    cfg.strategy.oi_change_min_pct = orig_oi_min
    return result


# ── report ────────────────────────────────────────────────────────────────────

def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def print_report(result: BtResult, coins: list[str], days: int, timeframe: str) -> None:
    trades = result.trades
    closed = [t for t in trades if t.exit_reason != ""]
    print(f"\n{'═'*70}")
    print(f"  BACKTEST  |  {', '.join(coins)}  |  {timeframe}  |  {days}d  |  ${result.initial_equity:,.0f}")
    print(f"{'═'*70}\n")

    if not closed:
        print("  No trades.\n")
        return

    hdr = f"{'#':>3}  {'Coin':<6} {'Side':>5}  {'Entry':>12}  {'Exit':>12}  {'PnL($)':>9}  {'PnL%':>7}  {'Hold':>6}  Exit"
    print(hdr)
    print("─" * len(hdr))
    for n, t in enumerate(closed, 1):
        hold = f"{t.hold_h:.1f}h" if t.hold_h >= 1 else f"{t.hold_h*60:.0f}m"
        pnl_s = f"{t.pnl_usd:+.2f}"
        pnl_p = f"{t.pnl_pct:+.2f}%"
        print(f"{n:>3}  {t.coin:<6} {t.side:>5}  {_fmt_ts(t.entry_time):>12}  {_fmt_ts(t.exit_time):>12}  "
              f"{pnl_s:>9}  {pnl_p:>7}  {hold:>6}  {t.exit_reason}")

    wins = [t for t in closed if t.pnl_usd > 0]
    losses = [t for t in closed if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in closed)
    winrate = len(wins) / len(closed) * 100 if closed else 0.0
    avg_win = mean(t.pnl_usd for t in wins) if wins else 0.0
    avg_loss = mean(t.pnl_usd for t in losses) if losses else 0.0
    profit_factor = (
        sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses))
        if losses and sum(t.pnl_usd for t in losses) != 0 else math.inf
    )
    avg_hold = mean(t.hold_h for t in closed)

    exit_counts: dict[str, int] = {}
    for t in closed:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

    peak = result.initial_equity
    max_dd = 0.0
    for _, eq in result.equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    final_equity = result.equity_curve[-1][1] if result.equity_curve else result.initial_equity

    print(f"\n{'─'*70}")
    print(f"  Trades    : {len(closed)}  (W:{len(wins)}  L:{len(losses)})")
    print(f"  Win rate  : {winrate:.1f}%")
    print(f"  Total PnL : ${total_pnl:+,.2f}  ({total_pnl/result.initial_equity*100:+.2f}%)")
    print(f"  Final eq  : ${final_equity:,.2f}")
    print(f"  Max DD    : {max_dd:.2f}%")
    print(f"  Profit F  : {profit_factor:.2f}")
    print(f"  Avg win   : ${avg_win:+.2f}   Avg loss: ${avg_loss:+.2f}")
    print(f"  Avg hold  : {avg_hold:.1f}h")
    print(f"  Exits     : {exit_counts}")
    print(f"{'═'*70}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(prog="python -m src.backtest.runner")
    p.add_argument("--coins", nargs="+", default=None)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--timeframe", default=None)
    args = p.parse_args()

    cfg = load_config()
    secrets = load_secrets()

    coins = args.coins or cfg.data.coins
    timeframe = args.timeframe or cfg.strategy.timeframe

    init_db(secrets.database_url)
    sf = make_session_factory(secrets.database_url)

    print(f"Loading data from DB ({timeframe}, {args.days}d) ...")
    t0 = time.time()
    cache = load_candle_cache(sf, coins, timeframe, args.days)
    print(f"Data loaded in {time.time()-t0:.1f}s")

    print(f"Running backtest: {coins} | {timeframe} | {args.days}d ...")
    t0 = time.time()
    result = run(sf, coins, timeframe, args.days, cfg, cache=cache)
    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s")

    print_report(result, coins, args.days, timeframe)


if __name__ == "__main__":
    main()
