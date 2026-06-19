from __future__ import annotations

from sqlalchemy.orm import Session

from src.storage.db import (
    AssetContextRow,
    CandleRow,
    EquitySnapshotRow,
    FeatureRow,
    FundingRateRow,
    PaperPositionRow,
    StrategySignalRow,
)


class CandleRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, row: CandleRow) -> None:
        try:
            self._s.merge(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def upsert_many(self, rows: list[CandleRow]) -> None:
        try:
            for row in rows:
                self._s.merge(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_latest(self, coin: str, interval: str, limit: int) -> list[CandleRow]:
        return (
            self._s.query(CandleRow)
            .filter(CandleRow.coin == coin, CandleRow.interval == interval)
            .order_by(CandleRow.open_time.desc())
            .limit(limit)
            .all()
        )


class FundingRateRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert_many(self, rows: list[FundingRateRow]) -> None:
        try:
            for row in rows:
                self._s.merge(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_latest(self, coin: str, limit: int) -> list[FundingRateRow]:
        return (
            self._s.query(FundingRateRow)
            .filter(FundingRateRow.coin == coin)
            .order_by(FundingRateRow.funding_time.desc())
            .limit(limit)
            .all()
        )


class AssetContextRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, row: AssetContextRow) -> None:
        try:
            self._s.merge(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_latest(self, coin: str) -> AssetContextRow | None:
        return (
            self._s.query(AssetContextRow)
            .filter(AssetContextRow.coin == coin)
            .order_by(AssetContextRow.timestamp.desc())
            .first()
        )


class FeatureRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, row: FeatureRow) -> None:
        try:
            self._s.merge(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_latest(self, coin: str, timeframe: str) -> FeatureRow | None:
        return (
            self._s.query(FeatureRow)
            .filter(FeatureRow.coin == coin, FeatureRow.timeframe == timeframe)
            .order_by(FeatureRow.feature_time.desc())
            .first()
        )


class StrategySignalRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def save(self, row: StrategySignalRow) -> None:
        try:
            self._s.add(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_active(self, coin: str) -> StrategySignalRow | None:
        return (
            self._s.query(StrategySignalRow)
            .filter(StrategySignalRow.coin == coin, StrategySignalRow.status == "ACTIVE")
            .first()
        )

    def list_today(self, since_ts: int) -> list[StrategySignalRow]:
        return (
            self._s.query(StrategySignalRow)
            .filter(StrategySignalRow.created_at >= since_ts)
            .order_by(StrategySignalRow.created_at.desc())
            .all()
        )


class PaperPositionRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def save(self, row: PaperPositionRow) -> None:
        try:
            self._s.add(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_open(self, coin: str) -> PaperPositionRow | None:
        return (
            self._s.query(PaperPositionRow)
            .filter(PaperPositionRow.coin == coin, PaperPositionRow.status == "OPEN")
            .first()
        )

    def list_open(self) -> list[PaperPositionRow]:
        return (
            self._s.query(PaperPositionRow)
            .filter(PaperPositionRow.status == "OPEN")
            .all()
        )

    def list_closed_today(self, since_ts: int) -> list[PaperPositionRow]:
        return (
            self._s.query(PaperPositionRow)
            .filter(
                PaperPositionRow.status == "CLOSED",
                PaperPositionRow.closed_at >= since_ts,
            )
            .all()
        )


class EquitySnapshotRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def save(self, row: EquitySnapshotRow) -> None:
        try:
            self._s.add(row)
            self._s.commit()
        except Exception:
            self._s.rollback()
            raise

    def get_today(self, since_ts: int) -> list[EquitySnapshotRow]:
        return (
            self._s.query(EquitySnapshotRow)
            .filter(EquitySnapshotRow.timestamp >= since_ts)
            .order_by(EquitySnapshotRow.timestamp.asc())
            .all()
        )
