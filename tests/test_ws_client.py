from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock

from src.hyperliquid.ws_client import MarketWSClient


def make_client() -> MarketWSClient:
    return MarketWSClient()


# ------------------------------------------------------------------
# Registration tests
# ------------------------------------------------------------------

def test_subscribe_candle_registers():
    client = make_client()
    cb = AsyncMock()
    client.subscribe_candle("BTC", "1h", cb)
    assert ("BTC", "1h") in client._candle_subs
    assert client._candle_subs[("BTC", "1h")] is cb


def test_subscribe_trades_registers():
    client = make_client()
    cb = AsyncMock()
    client.subscribe_trades("ETH", cb)
    assert "ETH" in client._trades_subs
    assert client._trades_subs["ETH"] is cb


# ------------------------------------------------------------------
# Dispatch tests
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_candle_calls_callback():
    client = make_client()
    cb = AsyncMock()
    client.subscribe_candle("BTC", "1h", cb)

    msg = json.dumps({
        "channel": "candle",
        "data": {
            "s": "BTC",
            "i": "1h",
            "t": 1_700_000_000_000,
            "T": 1_700_003_600_000,
            "o": "30000.0",
            "h": "30500.0",
            "l": "29900.0",
            "c": "30200.0",
            "v": "123.45",
            "x": False,
        },
    })

    await client._dispatch(msg)

    cb.assert_awaited_once()
    coin_arg, interval_arg, candle_arg = cb.call_args.args
    assert coin_arg == "BTC"
    assert interval_arg == "1h"
    assert candle_arg["open_time"] == 1_700_000_000
    assert candle_arg["close_time"] == 1_700_003_600
    assert candle_arg["open"] == 30000.0
    assert candle_arg["high"] == 30500.0
    assert candle_arg["low"] == 29900.0
    assert candle_arg["close"] == 30200.0
    assert candle_arg["volume"] == 123.45
    assert candle_arg["is_closed"] is False


@pytest.mark.asyncio
async def test_dispatch_trades_calls_callback():
    client = make_client()
    cb = AsyncMock()
    client.subscribe_trades("ETH", cb)

    trades = [
        {"coin": "ETH", "side": "B", "px": "2000.0", "sz": "1.0", "time": 1_700_000_000_000, "hash": "0xabc"},
        {"coin": "ETH", "side": "A", "px": "2001.0", "sz": "0.5", "time": 1_700_000_001_000, "hash": "0xdef"},
    ]
    msg = json.dumps({"channel": "trades", "data": trades})

    await client._dispatch(msg)

    cb.assert_awaited_once()
    coin_arg, trades_arg = cb.call_args.args
    assert coin_arg == "ETH"
    assert len(trades_arg) == 2
    assert trades_arg[0]["hash"] == "0xabc"


@pytest.mark.asyncio
async def test_dispatch_pong_no_error():
    client = make_client()
    msg = json.dumps({"channel": "pong"})
    # Must not raise
    await client._dispatch(msg)


@pytest.mark.asyncio
async def test_dispatch_candle_unregistered_no_error():
    client = make_client()
    msg = json.dumps({
        "channel": "candle",
        "data": {
            "s": "SOL", "i": "15m",
            "t": 0, "T": 0,
            "o": "0", "h": "0", "l": "0", "c": "0", "v": "0",
        },
    })
    # No callback registered for SOL/15m — must not raise
    await client._dispatch(msg)


@pytest.mark.asyncio
async def test_dispatch_all_mids_calls_price_callback():
    client = make_client()
    price_cb = AsyncMock()
    client.set_price_callback(price_cb)

    msg = json.dumps({
        "channel": "allMids",
        "data": {"mids": {"BTC": "30000.5", "ETH": "2000.1"}},
    })

    await client._dispatch(msg)

    price_cb.assert_awaited_once()
    mids_arg: dict = price_cb.call_args.args[0]
    assert mids_arg["BTC"] == pytest.approx(30000.5)
    assert mids_arg["ETH"] == pytest.approx(2000.1)
