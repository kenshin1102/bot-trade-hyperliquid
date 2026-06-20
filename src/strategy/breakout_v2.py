"""
Breakout_V2: retest/pullback entry.

Pattern (LONG):
  1. Range: candles[-(lb+bars):-bars]
  2. Breakout: any bar in candles[-bars:-1] closed above range_high
  3. No full retrace: all post-breakout bars closed above range_high - tol
  4. Retest (current bar): low touched range_high ± tol, close >= range_high

SL anchored to level, not entry price → tighter risk.
"""
from __future__ import annotations

import logging

from src.config.settings import RiskConfig, StrategyConfig
from src.storage.db import CandleRow
from src.strategy.breakout import BreakoutSignal
from src.strategy.feature_engine import Features
from src.strategy.regime import Regime, RegimeDetector

logger = logging.getLogger(__name__)


class BreakoutV2:
    def __init__(self, cfg: StrategyConfig, risk_cfg: RiskConfig, regime_detector: RegimeDetector) -> None:
        self._cfg = cfg
        self._risk = risk_cfg
        self._regime = regime_detector

    def evaluate(
        self,
        coin: str,
        features: Features,
        btc_features: Features | None,
        candles: list[CandleRow],
        current_price: float,
        current_spread_bps: float,
    ) -> BreakoutSignal | None:
        regime_score = self._regime.score(features, btc_features)
        regime = self._regime.classify(regime_score)
        if regime == Regime.NO_TRADE:
            return None

        cfg = self._cfg
        lb = cfg.breakout_lookback_candles
        bars = cfg.v2_max_bars_to_retest
        tol = features.atr * cfg.v2_retest_tol_atr

        # Need: lb (range) + bars (post-breakout window) + 1 (current bar)
        if len(candles) < lb + bars + 1:
            return None

        range_candles = candles[-(lb + bars):-bars]
        range_high = max(c.high for c in range_candles)
        range_low = min(c.low for c in range_candles)

        post_breakout = candles[-bars:-1]
        current = candles[-1]

        had_long_break = any(c.close > range_high for c in post_breakout)
        had_short_break = any(c.close < range_low for c in post_breakout)

        base_ok = (
            features.oi_change_pct >= cfg.oi_change_min_pct
            and current_spread_bps <= cfg.spread_max_bps
        )

        # LONG retest: breakout occurred, price held above level, current retests it
        is_long = (
            had_long_break
            and not had_short_break
            and all(c.close >= range_high - tol for c in post_breakout)  # held above level
            and current.low <= range_high + tol                           # touched the level
            and current.low >= range_high - tol                           # didn't break through
            and current.close >= range_high                               # closed above
            and features.funding_rate <= cfg.funding_max_pct
            and base_ok
        )

        # SHORT retest: breakout below, price held under level, current retests it
        is_short = (
            had_short_break
            and not had_long_break
            and all(c.close <= range_low + tol for c in post_breakout)
            and current.high >= range_low - tol
            and current.high <= range_low + tol
            and current.close <= range_low
            and features.funding_rate >= -cfg.funding_max_pct
            and base_ok
        )

        if not is_long and not is_short:
            return None

        # EMA spread filter (same as V1)
        if cfg.ema_spread_min_pct > 0:
            ema_spread = (features.ema_20 - features.ema_50) / features.ema_50 * 100
            if is_long and ema_spread < cfg.ema_spread_min_pct:
                return None
            if is_short and ema_spread > -cfg.ema_spread_min_pct:
                return None

        sl_dist = features.atr * self._risk.sl_atr_multiplier

        if is_long:
            sl_price = range_high - sl_dist
            tp_price = current_price + abs(current_price - sl_price) * self._risk.tp_rr
            side = "LONG"
            reason = "v2_retest_support"
        else:
            sl_price = range_low + sl_dist
            tp_price = current_price - abs(current_price - sl_price) * self._risk.tp_rr
            side = "SHORT"
            reason = "v2_retest_resistance"

        logger.info("%s: V2 %s score=%.1f", coin, side, regime_score)
        return BreakoutSignal(
            coin=coin,
            side=side,
            entry_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            regime_score=regime_score,
            volume_zscore=features.volume_zscore,
            oi_change_pct=features.oi_change_pct,
            reason=reason,
            regime=regime,
        )
