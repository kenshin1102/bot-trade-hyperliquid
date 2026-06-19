from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config.settings import RegimeConfig, RiskConfig, StrategyConfig
from src.storage.db import Base, CandleRow
from src.strategy.breakout import BreakoutV1
from src.strategy.feature_engine import FeatureEngine, Features
from src.strategy.regime import Regime, RegimeDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_factory(url: str = "sqlite:///:memory:"):
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _candle(coin: str, interval: str, open_time: int, close: float, volume: float = 100.0) -> CandleRow:
    return CandleRow(
        id=str(uuid.uuid4()),
        coin=coin,
        interval=interval,
        open_time=open_time,
        close_time=open_time + 899,
        open=close,
        high=close * 1.005,
        low=close * 0.995,
        close=close,
        volume=volume,
        created_at=int(time.time()),
    )


def _seed_candles(sf, coin: str, interval: str, prices: list[float], volumes: list[float] | None = None) -> None:
    from src.storage.repository import CandleRepo
    if volumes is None:
        volumes = [100.0] * len(prices)
    with sf() as s:
        repo = CandleRepo(s)
        for i, (p, v) in enumerate(zip(prices, volumes)):
            repo.upsert(_candle(coin, interval, i * 900, p, v))


def _features(
    coin: str = "ETH",
    ema_20: float = 2000.0,
    ema_50: float = 1900.0,
    atr: float = 50.0,
    volume_zscore: float = 2.0,
    oi_change_pct: float = 0.03,
    funding_rate: float = 0.0001,
    funding_percentile: float = 0.5,
) -> Features:
    return Features(
        coin=coin,
        timeframe="15m",
        feature_time=int(time.time()),
        ema_20=ema_20,
        ema_50=ema_50,
        atr=atr,
        volume_zscore=volume_zscore,
        oi_change_pct=oi_change_pct,
        funding_rate=funding_rate,
        funding_percentile=funding_percentile,
    )


# ---------------------------------------------------------------------------
# Feature engine tests
# ---------------------------------------------------------------------------

def test_compute_returns_none_when_insufficient_candles():
    sf = _make_session_factory()
    _seed_candles(sf, "BTC", "15m", [30000.0] * 30)  # only 30, need 51
    engine = FeatureEngine(sf)
    result = engine.compute("BTC", "15m")
    assert result is None


def test_compute_ema_increases_in_uptrend():
    sf = _make_session_factory()
    # Ascending prices: ema_20 should lag less than ema_50 → ema_20 > ema_50
    prices = [float(1000 + i * 10) for i in range(60)]
    _seed_candles(sf, "ETH", "15m", prices)
    engine = FeatureEngine(sf)
    result = engine.compute("ETH", "15m")
    assert result is not None
    assert result.ema_20 > result.ema_50


def test_volume_zscore_positive_on_spike():
    sf = _make_session_factory()
    prices = [float(2000 + i) for i in range(60)]
    # Base volumes vary slightly so stdev > 0; last candle has 5x spike
    import random
    random.seed(42)
    base_vols = [100.0 + random.uniform(-10, 10) for _ in range(59)]
    volumes = base_vols + [500.0]
    _seed_candles(sf, "SOL", "15m", prices, volumes)
    engine = FeatureEngine(sf)
    result = engine.compute("SOL", "15m")
    assert result is not None
    assert result.volume_zscore > 1.0


# ---------------------------------------------------------------------------
# Regime detector tests
# ---------------------------------------------------------------------------

def test_no_trade_regime_when_score_low():
    cfg = RegimeConfig(no_trade_below=30.0, small_size_below=50.0)
    detector = RegimeDetector(cfg)
    # Downtrend: ema_20 < ema_50; negative volume z; extreme funding; negative OI
    f = _features(ema_20=1800.0, ema_50=2000.0, volume_zscore=-2.0, oi_change_pct=-0.05, funding_percentile=0.0)
    score = detector.score(f, None)
    regime = detector.classify(score)
    assert score < 30.0
    assert regime == Regime.NO_TRADE


def test_normal_regime_when_score_mid():
    cfg = RegimeConfig(no_trade_below=30.0, small_size_below=50.0)
    detector = RegimeDetector(cfg)
    # Uptrend coin, neutral funding, mild volume, slight OI up
    f = _features(ema_20=2100.0, ema_50=2000.0, volume_zscore=1.0, oi_change_pct=0.01, funding_percentile=0.5)
    score = detector.score(f, None)
    regime = detector.classify(score)
    assert 50.0 <= score < 85.0
    assert regime == Regime.NORMAL


# ---------------------------------------------------------------------------
# Breakout strategy tests
# ---------------------------------------------------------------------------

def _make_candles(prices: list[float]) -> list[CandleRow]:
    return [
        CandleRow(
            id=str(uuid.uuid4()),
            coin="ETH",
            interval="15m",
            open_time=i * 900,
            close_time=i * 900 + 899,
            open=p,
            high=p * 1.01,
            low=p * 0.99,
            close=p,
            volume=100.0,
            created_at=int(time.time()),
        )
        for i, p in enumerate(prices)
    ]


def test_long_signal_on_break_high():
    cfg = StrategyConfig(breakout_lookback_candles=20, volume_zscore_min=1.5, oi_change_min_pct=0.02,
                         funding_max_pct=0.005, spread_max_bps=20.0)
    risk = RiskConfig(sl_atr_multiplier=1.5, tp_rr=2.0)
    regime_cfg = RegimeConfig(no_trade_below=30.0, small_size_below=50.0)
    detector = RegimeDetector(regime_cfg)
    strategy = BreakoutV1(cfg, risk, detector)

    # 21 candles: first 20 top out at 2000, last candle breaks above to 2100
    prices = [2000.0] * 20 + [2100.0]
    candles = _make_candles(prices)

    # Features: uptrend, volume spike, OI up, funding ok
    f = _features(ema_20=2050.0, ema_50=1900.0, atr=50.0, volume_zscore=2.0,
                  oi_change_pct=0.03, funding_rate=0.0001, funding_percentile=0.5)

    signal = strategy.evaluate("ETH", f, None, candles, 2100.0, 5.0)
    assert signal is not None
    assert signal.side == "LONG"
    assert signal.coin == "ETH"
    assert "break_high" in signal.reason
    assert signal.sl_price < 2100.0
    assert signal.tp_price > 2100.0


def test_no_signal_when_no_break():
    cfg = StrategyConfig(breakout_lookback_candles=20, volume_zscore_min=1.5, oi_change_min_pct=0.02,
                         funding_max_pct=0.005, spread_max_bps=20.0)
    risk = RiskConfig(sl_atr_multiplier=1.5, tp_rr=2.0)
    regime_cfg = RegimeConfig(no_trade_below=30.0, small_size_below=50.0)
    detector = RegimeDetector(regime_cfg)
    strategy = BreakoutV1(cfg, risk, detector)

    # All candles at same price, no breakout
    prices = [2000.0] * 21
    candles = _make_candles(prices)
    f = _features(ema_20=2050.0, ema_50=1900.0, atr=50.0, volume_zscore=2.0,
                  oi_change_pct=0.03, funding_rate=0.0001, funding_percentile=0.5)

    signal = strategy.evaluate("ETH", f, None, candles, 2000.0, 5.0)
    assert signal is None


def test_no_signal_on_no_trade_regime():
    cfg = StrategyConfig(breakout_lookback_candles=20, volume_zscore_min=1.5, oi_change_min_pct=0.02,
                         funding_max_pct=0.005, spread_max_bps=20.0)
    risk = RiskConfig(sl_atr_multiplier=1.5, tp_rr=2.0)
    # Set threshold so any score → NO_TRADE
    regime_cfg = RegimeConfig(no_trade_below=100.0, small_size_below=100.0)
    detector = RegimeDetector(regime_cfg)
    strategy = BreakoutV1(cfg, risk, detector)

    # Candles that would otherwise trigger long
    prices = [2000.0] * 20 + [2100.0]
    candles = _make_candles(prices)
    f = _features(ema_20=2050.0, ema_50=1900.0, atr=50.0, volume_zscore=2.0,
                  oi_change_pct=0.03, funding_rate=0.0001, funding_percentile=0.5)

    signal = strategy.evaluate("ETH", f, None, candles, 2100.0, 5.0)
    assert signal is None
