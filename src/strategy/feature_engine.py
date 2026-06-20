from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from statistics import mean, stdev

from src.storage.db import AssetContextRow, FeatureRow
from src.storage.repository import AssetContextRepo, CandleRepo, FeatureRepo, FundingRateRepo

logger = logging.getLogger(__name__)


@dataclass
class Features:
    coin: str
    timeframe: str
    feature_time: int
    ema_20: float
    ema_50: float
    atr: float
    atr_mean: float
    volume_zscore: float
    oi_change_pct: float
    funding_rate: float
    funding_percentile: float
    regime_score: float = 0.0


def _ema(prices: list[float], period: int) -> float:
    k = 2 / (period + 1)
    val = prices[0]
    for p in prices[1:]:
        val = p * k + val * (1 - k)
    return val


def _atr(candles: list) -> float:
    trs: list[float] = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    return mean(trs) if trs else 0.0


class FeatureEngine:
    def __init__(self, session_factory) -> None:
        self._sf = session_factory
        self._prev_oi: dict[str, float] = {}

    def compute(self, coin: str, timeframe: str) -> Features | None:
        with self._sf() as s:
            candles = CandleRepo(s).get_latest(coin, timeframe, 100)
            candles = sorted(candles, key=lambda c: c.open_time)

            if len(candles) < 51:
                logger.debug("Not enough candles for %s %s: %d", coin, timeframe, len(candles))
                return None

            closes = [c.close for c in candles]
            ema_20 = _ema(closes[-20:], 20)
            ema_50 = _ema(closes[-50:], 50)
            atr = _atr(candles[-15:])  # 14 TRs need 15 candles
            atr_mean = mean(c.high - c.low for c in candles[-30:]) if len(candles) >= 30 else atr

            last_20 = candles[-20:]
            vols = [c.volume for c in last_20]
            mean_v = mean(vols[:-1])
            std_v = stdev(vols[:-1]) if len(vols) > 2 else 0.0
            volume_zscore = (vols[-1] - mean_v) / std_v if std_v > 0 else 0.0

            ctx: AssetContextRow | None = AssetContextRepo(s).get_latest(coin)
            current_oi = ctx.open_interest if ctx else 0.0
            prev_oi = self._prev_oi.get(coin)
            if prev_oi is None or prev_oi == 0.0:
                oi_change_pct = 0.0
            else:
                oi_change_pct = (current_oi - prev_oi) / prev_oi
            self._prev_oi[coin] = current_oi

            funding_rows = FundingRateRepo(s).get_latest(coin, 1)
            funding_rate = funding_rows[0].rate if funding_rows else 0.0

            history = FundingRateRepo(s).get_latest(coin, 30 * 3)
            if history:
                funding_percentile = len([r for r in history if r.rate < funding_rate]) / len(history)
            else:
                funding_percentile = 0.5

            feature_time = candles[-1].close_time
            row = FeatureRow(
                id=f"{coin}_{timeframe}",
                coin=coin,
                timeframe=timeframe,
                feature_time=feature_time,
                ema_20=ema_20,
                ema_50=ema_50,
                atr=atr,
                volume_zscore=volume_zscore,
                oi_change_pct=oi_change_pct,
                funding_rate=funding_rate,
                funding_percentile=funding_percentile,
                regime_score=0.0,
                created_at=int(time.time()),
            )
            FeatureRepo(s).upsert(row)

        return Features(
            coin=coin,
            timeframe=timeframe,
            feature_time=feature_time,
            ema_20=ema_20,
            ema_50=ema_50,
            atr=atr,
            atr_mean=atr_mean,
            volume_zscore=volume_zscore,
            oi_change_pct=oi_change_pct,
            funding_rate=funding_rate,
            funding_percentile=funding_percentile,
        )
