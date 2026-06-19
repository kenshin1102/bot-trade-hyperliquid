"""Tests for backtest runner."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.backtest.runner import BtTrade, _check_exit, _close, _compute_features
from src.storage.db import CandleRow, FundingRateRow


def _make_candle(close: float, high: float | None = None, low: float | None = None,
                 volume: float = 1000.0, open_time: int = 0, close_time: int = 900) -> CandleRow:
    c = CandleRow()
    c.open = close * 0.999
    c.close = close
    c.high = high if high is not None else close * 1.005
    c.low = low if low is not None else close * 0.995
    c.volume = volume
    c.open_time = open_time
    c.close_time = close_time
    return c


def _make_candles(n: int, base: float = 100.0) -> list[CandleRow]:
    return [_make_candle(base + i * 0.01, open_time=i * 900, close_time=(i + 1) * 900) for i in range(n)]


def _make_funding(rate: float = 0.0001, n: int = 10) -> list[FundingRateRow]:
    rows = []
    for i in range(n):
        r = FundingRateRow()
        r.rate = rate
        r.funding_time = i * 28800
        rows.append(r)
    return rows


class TestCheckExit:
    def test_long_sl_hit(self):
        pos = BtTrade("BTC", "LONG", 0, 100.0, 95.0, 110.0, 1000.0, 0.0, 50.0, "NORMAL")
        candle = _make_candle(96.0, high=97.0, low=94.0)
        result = _check_exit(pos, candle)
        assert result == ("sl", 95.0)

    def test_long_tp_hit(self):
        pos = BtTrade("BTC", "LONG", 0, 100.0, 95.0, 110.0, 1000.0, 0.0, 50.0, "NORMAL")
        candle = _make_candle(111.0, high=112.0, low=109.0)
        result = _check_exit(pos, candle)
        assert result == ("tp", 110.0)

    def test_long_both_hit_sl_wins(self):
        pos = BtTrade("BTC", "LONG", 0, 100.0, 95.0, 110.0, 1000.0, 0.0, 50.0, "NORMAL")
        candle = _make_candle(100.0, high=115.0, low=90.0)
        result = _check_exit(pos, candle)
        assert result == ("sl", 95.0)

    def test_short_sl_hit(self):
        pos = BtTrade("BTC", "SHORT", 0, 100.0, 105.0, 90.0, 1000.0, 0.0, 50.0, "NORMAL")
        candle = _make_candle(104.0, high=106.0, low=103.0)
        result = _check_exit(pos, candle)
        assert result == ("sl", 105.0)

    def test_no_exit(self):
        pos = BtTrade("BTC", "LONG", 0, 100.0, 95.0, 110.0, 1000.0, 0.0, 50.0, "NORMAL")
        candle = _make_candle(102.0, high=103.0, low=101.0)
        result = _check_exit(pos, candle)
        assert result is None


class TestClose:
    def test_long_profit(self):
        pos = BtTrade("BTC", "LONG", 0, 100.0, 95.0, 110.0, 10000.0, 2.5, 50.0, "NORMAL")
        _close(pos, 110.0, 900, "tp", fee_bps=2.5)
        assert pos.pnl_usd > 0
        assert pos.exit_reason == "tp"
        assert pos.exit_price == 110.0

    def test_long_loss(self):
        pos = BtTrade("BTC", "LONG", 0, 100.0, 95.0, 110.0, 10000.0, 2.5, 50.0, "NORMAL")
        _close(pos, 95.0, 900, "sl", fee_bps=2.5)
        assert pos.pnl_usd < 0

    def test_short_profit(self):
        pos = BtTrade("BTC", "SHORT", 0, 100.0, 105.0, 90.0, 10000.0, 2.5, 50.0, "NORMAL")
        _close(pos, 90.0, 900, "tp", fee_bps=2.5)
        assert pos.pnl_usd > 0


class TestComputeFeatures:
    def test_returns_none_if_not_enough_candles(self):
        candles = _make_candles(30)
        result = _compute_features("BTC", "15m", candles, _make_funding())
        assert result is None

    def test_returns_features_with_enough_candles(self):
        candles = _make_candles(60, base=50000.0)
        funding = _make_funding(rate=0.0001, n=30)
        result = _compute_features("BTC", "15m", candles, funding)
        assert result is not None
        assert result.coin == "BTC"
        assert result.ema_20 > 0
        assert result.ema_50 > 0
        assert result.atr >= 0
        assert result.oi_change_pct == 0.0  # no OI in backtest
