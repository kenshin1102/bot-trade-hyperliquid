"""Tests for src/execution/paper.py using in-memory SQLite + mock notifier."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config.settings import ExecutionConfig, RiskConfig
from src.execution.paper import PaperEngine
from src.storage.db import Base
from src.strategy.breakout import BreakoutSignal
from src.strategy.regime import Regime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def mock_notifier():
    notifier = AsyncMock()
    notifier.send = AsyncMock()
    return notifier


@pytest.fixture
def paper(session_factory, mock_notifier):
    risk = RiskConfig(
        max_risk_per_trade_pct=1.0,
        sl_atr_multiplier=1.5,
        tp_rr=2.0,
        max_daily_loss_pct=2.0,
        max_concurrent_positions=3,
        emergency_stop=False,
    )
    execution = ExecutionConfig(
        mode="paper",
        account_balance=10000.0,
        fee_taker_bps=2.5,
        slippage_bps=5.0,
    )
    return PaperEngine(risk, execution, mock_notifier, session_factory)


def _make_signal(
    coin: str = "BTC",
    side: str = "LONG",
    entry_price: float = 50000.0,
    sl_price: float = 49000.0,
    tp_price: float = 52000.0,
    regime: Regime = Regime.NORMAL,
) -> BreakoutSignal:
    return BreakoutSignal(
        coin=coin,
        side=side,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        regime_score=70.0,
        volume_zscore=2.0,
        oi_change_pct=0.03,
        reason="break_high + volume_spike + oi_up",
        regime=regime,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_signal_opens_position(paper, mock_notifier):
    paper._prices["BTC"] = 50000.0
    signal = _make_signal(coin="BTC")
    result = await paper.on_signal(signal, "sig:BTC:1")

    assert result is True
    assert "BTC" in paper._positions
    pos = paper._positions["BTC"]
    assert pos.side == "LONG"
    assert pos.size_notional > 0
    assert pos.fee_usd > 0
    mock_notifier.send.assert_called_once()


@pytest.mark.asyncio
async def test_on_signal_skips_duplicate_coin(paper):
    paper._prices["BTC"] = 50000.0
    signal = _make_signal(coin="BTC")
    await paper.on_signal(signal, "sig:BTC:1")

    result = await paper.on_signal(signal, "sig:BTC:2")
    assert result is False
    assert len(paper._positions) == 1


@pytest.mark.asyncio
async def test_on_signal_skips_max_positions(paper):
    coins = ["BTC", "ETH", "SOL"]
    for coin in coins:
        paper._prices[coin] = 1000.0
        sig = _make_signal(
            coin=coin,
            entry_price=1000.0,
            sl_price=950.0,
            tp_price=1100.0,
        )
        await paper.on_signal(sig, f"sig:{coin}:1")

    assert len(paper._positions) == 3

    paper._prices["AVAX"] = 30.0
    sig4 = _make_signal(
        coin="AVAX",
        entry_price=30.0,
        sl_price=28.0,
        tp_price=34.0,
    )
    result = await paper.on_signal(sig4, "sig:AVAX:1")
    assert result is False
    assert len(paper._positions) == 3


@pytest.mark.asyncio
async def test_sl_hit_closes_position(paper, mock_notifier):
    paper._prices["BTC"] = 50000.0
    signal = _make_signal(coin="BTC", sl_price=49000.0, tp_price=52000.0)
    await paper.on_signal(signal, "sig:BTC:1")

    mock_notifier.send.reset_mock()

    # Price drops below SL
    await paper.on_price_update({"BTC": 48500.0})

    assert "BTC" not in paper._positions
    # Notifier called for close alert
    mock_notifier.send.assert_called_once()
    call_args = mock_notifier.send.call_args[0]
    assert "Stop Loss" in call_args[1]


@pytest.mark.asyncio
async def test_tp_hit_closes_position(paper, mock_notifier):
    paper._prices["BTC"] = 50000.0
    signal = _make_signal(coin="BTC", sl_price=49000.0, tp_price=52000.0)
    await paper.on_signal(signal, "sig:BTC:1")

    mock_notifier.send.reset_mock()

    # Price rises above TP
    await paper.on_price_update({"BTC": 52500.0})

    assert "BTC" not in paper._positions
    mock_notifier.send.assert_called_once()
    call_args = mock_notifier.send.call_args[0]
    assert "Take Profit" in call_args[1]


@pytest.mark.asyncio
async def test_emergency_stop_blocks_signal(paper):
    paper._risk.emergency_stop = True
    paper._prices["BTC"] = 50000.0
    signal = _make_signal(coin="BTC")

    result = await paper.on_signal(signal, "sig:BTC:1")
    assert result is False
    assert len(paper._positions) == 0
