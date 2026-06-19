from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.storage.db import (
    Base,
    CandleRow,
    EquitySnapshotRow,
    PaperPositionRow,
    StrategySignalRow,
    init_db,
    make_engine,
)
from src.storage.repository import (
    CandleRepo,
    EquitySnapshotRepo,
    PaperPositionRepo,
    StrategySignalRepo,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def test_init_db_creates_tables():
    init_db("sqlite://")


def test_candle_upsert_and_get_latest(session: Session):
    repo = CandleRepo(session)
    now = int(time.time())
    row = CandleRow(
        id="BTC:15m:1000",
        coin="BTC",
        interval="15m",
        open_time=1000,
        close_time=1900,
        open=30000.0,
        high=30500.0,
        low=29800.0,
        close=30200.0,
        volume=100.0,
        created_at=now,
    )
    repo.upsert(row)

    results = repo.get_latest("BTC", "15m", 10)
    assert len(results) == 1
    assert results[0].id == "BTC:15m:1000"
    assert results[0].close == 30200.0


def test_candle_upsert_replaces_existing(session: Session):
    repo = CandleRepo(session)
    now = int(time.time())
    row = CandleRow(
        id="ETH:1h:2000",
        coin="ETH",
        interval="1h",
        open_time=2000,
        close_time=5600,
        open=2000.0,
        high=2100.0,
        low=1950.0,
        close=2050.0,
        volume=50.0,
        created_at=now,
    )
    repo.upsert(row)

    updated = CandleRow(
        id="ETH:1h:2000",
        coin="ETH",
        interval="1h",
        open_time=2000,
        close_time=5600,
        open=2000.0,
        high=2200.0,
        low=1950.0,
        close=2150.0,
        volume=60.0,
        created_at=now,
    )
    repo.upsert(updated)

    results = repo.get_latest("ETH", "1h", 10)
    assert len(results) == 1
    assert results[0].close == 2150.0


def test_paper_position_save_and_get_open(session: Session):
    repo = PaperPositionRepo(session)
    now = int(time.time())
    row = PaperPositionRow(
        id="BTC:LONG:1000",
        signal_id=None,
        coin="BTC",
        side="LONG",
        entry_price=30000.0,
        size_notional=1000.0,
        sl_price=29000.0,
        tp_price=32000.0,
        status="OPEN",
        opened_at=now,
        closed_at=None,
        exit_price=None,
        exit_reason=None,
        pnl_usd=None,
        fee_usd=None,
    )
    repo.save(row)

    result = repo.get_open("BTC")
    assert result is not None
    assert result.id == "BTC:LONG:1000"
    assert result.side == "LONG"
    assert result.status == "OPEN"


def test_paper_position_get_open_returns_none_when_closed(session: Session):
    repo = PaperPositionRepo(session)
    now = int(time.time())
    row = PaperPositionRow(
        id="ETH:SHORT:2000",
        signal_id=None,
        coin="ETH",
        side="SHORT",
        entry_price=2000.0,
        size_notional=500.0,
        sl_price=2100.0,
        tp_price=1800.0,
        status="CLOSED",
        opened_at=now - 3600,
        closed_at=now,
        exit_price=1850.0,
        exit_reason="tp",
        pnl_usd=75.0,
        fee_usd=1.0,
    )
    repo.save(row)

    result = repo.get_open("ETH")
    assert result is None


def test_strategy_signal_save_and_list_today(session: Session):
    repo = StrategySignalRepo(session)
    now = int(time.time())

    old_signal = StrategySignalRow(
        id="Breakout_V1:BTC:100",
        strategy="Breakout_V1",
        coin="BTC",
        side="LONG",
        entry_price=30000.0,
        sl_price=29000.0,
        tp_price=32000.0,
        regime_score=0.7,
        volume_zscore=1.5,
        oi_change_pct=0.03,
        reason="break_high + volume_spike",
        status="CLOSED",
        reject_reason="",
        created_at=100,
        closed_at=200,
    )
    new_signal = StrategySignalRow(
        id=f"Breakout_V1:ETH:{now}",
        strategy="Breakout_V1",
        coin="ETH",
        side="SHORT",
        entry_price=2000.0,
        sl_price=2100.0,
        tp_price=1800.0,
        regime_score=0.5,
        volume_zscore=2.0,
        oi_change_pct=-0.02,
        reason="break_low + oi_down",
        status="ACTIVE",
        reject_reason="",
        created_at=now,
        closed_at=None,
    )
    repo.save(old_signal)
    repo.save(new_signal)

    since = now - 60
    results = repo.list_today(since)
    assert len(results) == 1
    assert results[0].coin == "ETH"
    assert results[0].status == "ACTIVE"


def test_strategy_signal_get_active(session: Session):
    repo = StrategySignalRepo(session)
    now = int(time.time())
    row = StrategySignalRow(
        id=f"Breakout_V1:BTC:{now}",
        strategy="Breakout_V1",
        coin="BTC",
        side="LONG",
        entry_price=30000.0,
        sl_price=29000.0,
        tp_price=32000.0,
        regime_score=0.8,
        volume_zscore=1.8,
        oi_change_pct=0.05,
        reason="break_high + volume_spike + oi_up",
        status="ACTIVE",
        reject_reason="",
        created_at=now,
        closed_at=None,
    )
    repo.save(row)

    result = repo.get_active("BTC")
    assert result is not None
    assert result.side == "LONG"

    result_none = repo.get_active("ETH")
    assert result_none is None


def test_equity_snapshot_save_and_get_today(session: Session):
    repo = EquitySnapshotRepo(session)
    now = int(time.time())

    old_snap = EquitySnapshotRow(
        timestamp=now - 86400,
        equity=9900.0,
        unrealized_pnl=0.0,
        realized_pnl_today=-100.0,
        max_drawdown_today=0.01,
        open_positions=0,
    )
    new_snap = EquitySnapshotRow(
        timestamp=now,
        equity=10050.0,
        unrealized_pnl=50.0,
        realized_pnl_today=0.0,
        max_drawdown_today=0.0,
        open_positions=1,
    )
    repo.save(old_snap)
    repo.save(new_snap)

    results = repo.get_today(now - 60)
    assert len(results) == 1
    assert results[0].equity == 10050.0
    assert results[0].open_positions == 1
