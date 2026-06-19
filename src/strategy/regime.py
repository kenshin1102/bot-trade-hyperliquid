from __future__ import annotations

import logging
from enum import Enum

from src.config.settings import RegimeConfig
from src.strategy.feature_engine import Features

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    NO_TRADE = "NO_TRADE"
    SMALL = "SMALL"
    NORMAL = "NORMAL"
    STRONG = "STRONG"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class RegimeDetector:
    def __init__(self, cfg: RegimeConfig) -> None:
        self._cfg = cfg

    def score(self, coin_features: Features, btc_features: Features | None) -> float:
        ref = btc_features if btc_features is not None else coin_features

        # BTC trend: is price above EMA50?
        # We approximate current price as ema_20 (latest available proxy)
        btc_trend_score = 70.0 if ref.ema_20 > ref.ema_50 else 30.0

        # Coin trend
        close_proxy = coin_features.ema_20  # close ~ ema_20 when very fresh
        if coin_features.ema_20 > coin_features.ema_50:
            coin_trend_score = 80.0
        elif coin_features.ema_20 < coin_features.ema_50:
            coin_trend_score = 20.0
        else:
            coin_trend_score = 50.0

        # Volume z-score clamped [-2, 4] → [0, 100]
        vz = _clamp(coin_features.volume_zscore, -2.0, 4.0)
        volume_score = (vz + 2.0) / 6.0 * 100.0

        # Funding: neutral (0.5) = 100; extremes (0.0 or 1.0) = 0
        funding_score = _clamp(100.0 - abs(coin_features.funding_percentile - 0.5) * 200.0, 0.0, 100.0)

        # OI change: -5% → 0, +5% → 100
        oi_score = _clamp((coin_features.oi_change_pct + 0.05) / 0.10 * 100.0, 0.0, 100.0)

        cfg = self._cfg
        total = (
            btc_trend_score * cfg.btc_trend_weight
            + coin_trend_score * cfg.coin_trend_weight
            + volume_score * cfg.volume_weight
            + funding_score * cfg.funding_weight
            + oi_score * cfg.oi_weight
        )
        logger.debug(
            "Regime score for %s: %.1f (btc=%.1f coin=%.1f vol=%.1f fund=%.1f oi=%.1f)",
            coin_features.coin, total, btc_trend_score, coin_trend_score,
            volume_score, funding_score, oi_score,
        )
        return total

    def classify(self, score: float) -> Regime:
        if score < self._cfg.no_trade_below:
            return Regime.NO_TRADE
        if score < self._cfg.small_size_below:
            return Regime.SMALL
        if score < 85:
            return Regime.NORMAL
        return Regime.STRONG
