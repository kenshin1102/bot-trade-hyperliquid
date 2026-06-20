from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config.settings import RiskConfig, StrategyConfig
from src.storage.db import CandleRow
from src.strategy.feature_engine import Features
from src.strategy.regime import Regime, RegimeDetector

logger = logging.getLogger(__name__)


@dataclass
class BreakoutSignal:
    coin: str
    side: str           # "LONG" | "SHORT"
    entry_price: float
    sl_price: float
    tp_price: float
    regime_score: float
    volume_zscore: float
    oi_change_pct: float
    reason: str
    regime: Regime


class BreakoutV1:
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
            logger.debug("%s: NO_TRADE regime (score=%.1f), skip", coin, regime_score)
            return None

        cfg = self._cfg
        lookback = cfg.breakout_lookback_candles
        confirm = cfg.breakout_confirm_bars

        # With 2-bar confirm we need one extra candle: range from [-(lb+2):-2],
        # breakout bar at [-2], confirmation bar at [-1].
        min_len = lookback + confirm
        if len(candles) < min_len:
            logger.debug("%s: not enough candles (%d < %d)", coin, len(candles), min_len)
            return None

        if confirm >= 2:
            range_candles = candles[-(lookback + 2):-2]
            breakout_bar = candles[-2]
            current = candles[-1]
        else:
            range_candles = candles[-(lookback + 1):-1]
            breakout_bar = candles[-1]
            current = candles[-1]

        range_high = max(c.high for c in range_candles)
        range_low = min(c.low for c in range_candles)

        base_ok = (
            features.volume_zscore >= cfg.volume_zscore_min
            and features.oi_change_pct >= cfg.oi_change_min_pct
            and current_spread_bps <= cfg.spread_max_bps
        )
        is_long = (
            breakout_bar.close > range_high
            and (confirm < 2 or current.close > range_high)
            and features.funding_rate <= cfg.funding_max_pct
            and base_ok
        )
        is_short = (
            breakout_bar.close < range_low
            and (confirm < 2 or current.close < range_low)
            and features.funding_rate >= -cfg.funding_max_pct
            and base_ok
        )

        if not is_long and not is_short:
            return None

        # EMA spread filter: skip if market is too flat (choppy)
        if cfg.ema_spread_min_pct > 0:
            ema_spread = (features.ema_20 - features.ema_50) / features.ema_50 * 100
            if is_long and ema_spread < cfg.ema_spread_min_pct:
                return None
            if is_short and ema_spread > -cfg.ema_spread_min_pct:
                return None

        # ATR expansion filter: skip if volatility is contracting
        if cfg.atr_expansion_min > 0 and features.atr_mean > 0:
            if features.atr < features.atr_mean * cfg.atr_expansion_min:
                return None

        sl_distance = features.atr * self._risk.sl_atr_multiplier

        if is_long:
            sl_price = current_price - sl_distance
            tp_price = current_price + sl_distance * self._risk.tp_rr
            reason = "break_high"
            if features.volume_zscore >= cfg.volume_zscore_min:
                reason += " + volume_spike"
            if features.oi_change_pct >= cfg.oi_change_min_pct:
                reason += " + oi_up"
            side = "LONG"
        else:
            sl_price = current_price + sl_distance
            tp_price = current_price - sl_distance * self._risk.tp_rr
            reason = "break_low"
            if features.volume_zscore >= cfg.volume_zscore_min:
                reason += " + volume_spike"
            if features.oi_change_pct >= cfg.oi_change_min_pct:
                reason += " + oi_up"
            side = "SHORT"

        logger.info("%s: %s signal score=%.1f reason=%s", coin, side, regime_score, reason)
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
