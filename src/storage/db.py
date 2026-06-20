from __future__ import annotations

from sqlalchemy import Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class CandleRow(Base):
    __tablename__ = "candles"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    coin: Mapped[str] = mapped_column(String, index=True)
    interval: Mapped[str] = mapped_column(String)
    open_time: Mapped[int] = mapped_column(Integer, index=True)
    close_time: Mapped[int] = mapped_column(Integer)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    created_at: Mapped[int] = mapped_column(Integer)


class FundingRateRow(Base):
    __tablename__ = "funding_rates"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    coin: Mapped[str] = mapped_column(String, index=True)
    funding_time: Mapped[int] = mapped_column(Integer, index=True)
    rate: Mapped[float] = mapped_column(Float)
    created_at: Mapped[int] = mapped_column(Integer)


class AssetContextRow(Base):
    __tablename__ = "asset_contexts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    coin: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[int] = mapped_column(Integer, index=True)
    mark_price: Mapped[float] = mapped_column(Float)
    open_interest: Mapped[float] = mapped_column(Float)
    created_at: Mapped[int] = mapped_column(Integer)


class FeatureRow(Base):
    __tablename__ = "features"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    coin: Mapped[str] = mapped_column(String, index=True)
    timeframe: Mapped[str] = mapped_column(String)
    feature_time: Mapped[int] = mapped_column(Integer, index=True)
    ema_20: Mapped[float] = mapped_column(Float)
    ema_50: Mapped[float] = mapped_column(Float)
    atr: Mapped[float] = mapped_column(Float)
    volume_zscore: Mapped[float] = mapped_column(Float)
    oi_change_pct: Mapped[float] = mapped_column(Float)
    funding_rate: Mapped[float] = mapped_column(Float)
    funding_percentile: Mapped[float] = mapped_column(Float)
    regime_score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[int] = mapped_column(Integer)


class StrategySignalRow(Base):
    __tablename__ = "strategy_signals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy: Mapped[str] = mapped_column(String)
    coin: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    entry_price: Mapped[float] = mapped_column(Float)
    sl_price: Mapped[float] = mapped_column(Float)
    tp_price: Mapped[float] = mapped_column(Float)
    regime_score: Mapped[float] = mapped_column(Float)
    volume_zscore: Mapped[float] = mapped_column(Float)
    oi_change_pct: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, index=True)
    closed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PaperPositionRow(Base):
    __tablename__ = "paper_positions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    coin: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    entry_price: Mapped[float] = mapped_column(Float)
    size_notional: Mapped[float] = mapped_column(Float)
    sl_price: Mapped[float] = mapped_column(Float)
    tp_price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String)
    opened_at: Mapped[int] = mapped_column(Integer, index=True)
    closed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[int] = mapped_column(Integer, index=True)
    equity: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    realized_pnl_today: Mapped[float] = mapped_column(Float)
    max_drawdown_today: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)


class UniverseSnapshotRow(Base):
    __tablename__ = "universe_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # f"{snapshot_time}:{coin}"
    snapshot_time: Mapped[int] = mapped_column(Integer, index=True)  # floored to hour
    coin: Mapped[str] = mapped_column(String, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    volume_24h_usd: Mapped[float] = mapped_column(Float)


def make_engine(database_url: str):
    return create_engine(database_url)


def make_session_factory(database_url: str):
    return sessionmaker(bind=make_engine(database_url), expire_on_commit=False)


def init_db(database_url: str) -> None:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
