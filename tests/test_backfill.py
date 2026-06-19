from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.data.backfill import backfill_asset_contexts, backfill_candles, backfill_funding
from src.storage.db import AssetContextRow, Base, CandleRow, FundingRateRow
from src.storage.repository import AssetContextRepo, CandleRepo, FundingRateRepo


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _fake_candles() -> list[dict]:
    now = int(time.time())
    return [
        {"open_time": now - 900 * i, "close_time": now - 900 * i + 899,
         "open": 30000.0, "high": 30500.0, "low": 29800.0, "close": 30200.0, "volume": 100.0}
        for i in range(3)
    ]


@pytest.mark.asyncio
async def test_backfill_candles_inserts_rows(session: Session) -> None:
    client = AsyncMock()
    client.get_candles = AsyncMock(return_value=_fake_candles())
    repo = CandleRepo(session)

    await backfill_candles(client, repo, coins=["BTC"], intervals=["15m"], days=1)

    rows = repo.get_latest("BTC", "15m", 10)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_backfill_candles_upserts_not_duplicates(session: Session) -> None:
    client = AsyncMock()
    client.get_candles = AsyncMock(return_value=_fake_candles())
    repo = CandleRepo(session)

    await backfill_candles(client, repo, coins=["BTC"], intervals=["15m"], days=1)
    await backfill_candles(client, repo, coins=["BTC"], intervals=["15m"], days=1)

    rows = repo.get_latest("BTC", "15m", 20)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_backfill_funding_inserts_rows(session: Session) -> None:
    now = int(time.time())
    fake_rates = [
        {"coin": "BTC", "funding_time": now - 3600 * i, "rate": 0.0001 * (i + 1)}
        for i in range(4)
    ]
    client = AsyncMock()
    client.get_funding_history = AsyncMock(return_value=fake_rates)
    repo = FundingRateRepo(session)

    await backfill_funding(client, repo, coins=["BTC"], days=1)

    rows = repo.get_latest("BTC", 10)
    assert len(rows) == 4
    assert all(r.coin == "BTC" for r in rows)


@pytest.mark.asyncio
async def test_backfill_asset_contexts_saves_latest(session: Session) -> None:
    fake_ctxs = [
        {"coin": "BTC", "mark_price": 30000.0, "open_interest": 5000.0},
        {"coin": "ETH", "mark_price": 2000.0, "open_interest": 3000.0},
        {"coin": "SOL", "mark_price": 100.0, "open_interest": 1000.0},
        {"coin": "DOGE", "mark_price": 0.1, "open_interest": 500.0},  # not in our list
    ]
    client = AsyncMock()
    client.get_asset_contexts = AsyncMock(return_value=fake_ctxs)
    repo = AssetContextRepo(session)

    coins = ["BTC", "ETH", "SOL"]
    await backfill_asset_contexts(client, repo, coins=coins)

    for coin in coins:
        row = repo.get_latest(coin)
        assert row is not None, f"Expected AssetContextRow for {coin}"
        assert row.coin == coin

    # DOGE was not in our coins list — should not be saved
    doge_row = repo.get_latest("DOGE")
    assert doge_row is None
