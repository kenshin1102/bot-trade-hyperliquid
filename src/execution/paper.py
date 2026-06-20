"""Paper execution engine with realistic fee/slippage fill model."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.config.settings import ExecutionConfig, RiskConfig
from src.monitoring.notifier import Notifier
from src.storage.db import EquitySnapshotRow, PaperPositionRow, StrategySignalRow
from src.storage.repository import EquitySnapshotRepo, PaperPositionRepo, StrategySignalRepo
from src.strategy.breakout import BreakoutSignal
from src.strategy.regime import Regime

logger = logging.getLogger("execution.paper")

_SNAPSHOT_INTERVAL_S = 900  # 15 minutes


@dataclass
class PaperPosition:
    id: str
    coin: str
    side: str           # "LONG" | "SHORT"
    entry_price: float  # fill price (after slippage + spread)
    size_notional: float
    sl_price: float
    tp_price: float
    signal_id: str
    opened_at: int
    fee_usd: float


class PaperEngine:
    def __init__(
        self,
        risk: RiskConfig,
        execution: ExecutionConfig,
        notifier: Notifier,
        session_factory=None,
    ) -> None:
        self._risk = risk
        self._exec = execution
        self._notifier = notifier
        self._sf = session_factory
        self._positions: dict[str, PaperPosition] = {}  # coin → position
        self._prices: dict[str, float] = {}
        self._realized_pnl_today: float = 0.0
        self._max_drawdown_today: float = 0.0
        self._peak_equity: float = execution.account_balance
        self._last_snapshot_ts: int = 0

        if session_factory is not None:
            self._hydrate_from_db()

    # ------------------------------------------------------------------
    # Public callbacks
    # ------------------------------------------------------------------

    async def on_price_update(self, prices: dict[str, float]) -> None:
        self._prices.update(prices)

        for coin in list(self._positions):
            price = prices.get(coin)
            if price is None:
                continue
            pos = self._positions[coin]
            if pos.side == "LONG":
                if price <= pos.sl_price:
                    await self._close(coin, price, "sl")
                elif price >= pos.tp_price:
                    await self._close(coin, price, "tp")
            else:
                if price >= pos.sl_price:
                    await self._close(coin, price, "sl")
                elif price <= pos.tp_price:
                    await self._close(coin, price, "tp")

        now = int(time.time())
        if now - self._last_snapshot_ts >= _SNAPSHOT_INTERVAL_S and self._sf is not None:
            self._last_snapshot_ts = now
            self._save_equity_snapshot(now)

    async def on_signal(self, signal: BreakoutSignal, signal_id: str) -> bool:
        """Returns True if position opened, False if skipped by risk gate."""
        coin = signal.coin

        # Risk gate — each branch records reject reason for DB persistence
        reject_reason: str | None = None

        if self._risk.emergency_stop:
            reject_reason = "emergency_stop"
        elif coin in self._positions:
            reject_reason = "duplicate_position"
        elif len(self._positions) >= self._risk.max_concurrent_positions:
            reject_reason = "max_positions"
        elif self._realized_pnl_today <= -(self._exec.account_balance * self._risk.max_daily_loss_pct / 100):
            reject_reason = "daily_loss_limit"
        elif signal.regime == Regime.NO_TRADE:
            reject_reason = "no_trade_regime"
        elif self._prices.get(coin) is None:
            reject_reason = "no_price"

        if reject_reason is not None:
            logger.warning("paper: reject %s — %s", coin, reject_reason)
            self._save_rejected_signal(signal, signal_id, reject_reason)
            return False

        price = self._prices[coin]

        # Fill price with slippage + fee
        adj_bps = (self._exec.slippage_bps + self._exec.fee_taker_bps) / 10000
        if signal.side == "LONG":
            fill_price = price * (1 + adj_bps)
        else:
            fill_price = price * (1 - adj_bps)

        # Position sizing from SL distance
        sl_distance_pct = abs(fill_price - signal.sl_price) / fill_price
        if sl_distance_pct <= 0:
            logger.warning("paper: zero SL distance for %s, skip", coin)
            self._save_rejected_signal(signal, signal_id, "zero_sl_distance")
            return False

        risk_usd = self._exec.account_balance * self._risk.max_risk_per_trade_pct / 100
        size_notional = risk_usd / sl_distance_pct
        if size_notional <= 0:
            return False

        fee_usd = size_notional * self._exec.fee_taker_bps / 10000

        pos = PaperPosition(
            id=signal_id,
            coin=coin,
            side=signal.side,
            entry_price=fill_price,
            size_notional=size_notional,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            signal_id=signal_id,
            opened_at=int(time.time()),
            fee_usd=fee_usd,
        )
        self._positions[coin] = pos

        # Persist to DB
        if self._sf is not None:
            s = self._sf()
            try:
                PaperPositionRepo(s).save(PaperPositionRow(
                    id=pos.id,
                    signal_id=pos.signal_id,
                    coin=coin,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    size_notional=pos.size_notional,
                    sl_price=pos.sl_price,
                    tp_price=pos.tp_price,
                    status="OPEN",
                    opened_at=pos.opened_at,
                    fee_usd=pos.fee_usd,
                ))
                StrategySignalRepo(s).save(StrategySignalRow(
                    id=signal_id,
                    strategy="Breakout_V1",
                    coin=coin,
                    side=signal.side,
                    entry_price=fill_price,
                    sl_price=signal.sl_price,
                    tp_price=signal.tp_price,
                    regime_score=signal.regime_score,
                    volume_zscore=signal.volume_zscore,
                    oi_change_pct=signal.oi_change_pct,
                    reason=signal.reason,
                    status="ACTIVE",
                    reject_reason="",
                    created_at=pos.opened_at,
                ))
            finally:
                s.close()

        logger.info(
            "paper OPEN %s %s fill=%.4f size=$%.0f sl=%.4f tp=%.4f fee=%.2f",
            coin, pos.side, fill_price, size_notional, pos.sl_price, pos.tp_price, fee_usd,
        )
        sl_pct = sl_distance_pct * 100
        coin_size = size_notional / fill_price if fill_price else 0
        notional_fmt = f"~${size_notional/1000:.0f}k" if size_notional >= 1000 else f"~${size_notional:.0f}"
        await self._notifier.send("info", (
            f"📄 PAPER OPEN\n\n"
            f"{coin} {signal.side}\n"
            f"Entry: ${fill_price:,.4f}\n"
            f"Size: {coin_size:.4g} {coin} ({notional_fmt})\n"
            f"SL: ${signal.sl_price:,.4f} ({sl_pct:.2f}%)\n"
            f"TP: ${signal.tp_price:,.4f}\n"
            f"Fee: ${fee_usd:.2f}\n"
            f"Regime: {signal.regime.value} ({signal.regime_score:.1f})\n"
            f"Reason: {signal.reason}"
        ))
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _hydrate_from_db(self) -> None:
        """Restore open positions and today's realized PnL from DB on startup."""
        s = self._sf()
        try:
            open_rows = PaperPositionRepo(s).list_open()
            for row in open_rows:
                self._positions[row.coin] = PaperPosition(
                    id=row.id,
                    coin=row.coin,
                    side=row.side,
                    entry_price=row.entry_price,
                    size_notional=row.size_notional,
                    sl_price=row.sl_price,
                    tp_price=row.tp_price,
                    signal_id=row.signal_id or "",
                    opened_at=row.opened_at,
                    fee_usd=row.fee_usd or 0.0,
                )
            since_ts = int(time.time()) - 86400
            closed_today = PaperPositionRepo(s).list_closed_today(since_ts)
            self._realized_pnl_today = sum(r.pnl_usd or 0.0 for r in closed_today)
            if open_rows or closed_today:
                logger.info(
                    "paper: hydrated %d open positions, realized_pnl_today=%.2f",
                    len(open_rows), self._realized_pnl_today,
                )
        except Exception as exc:
            logger.warning("paper: hydration failed: %s", exc)
        finally:
            s.close()

    def _save_rejected_signal(self, signal: BreakoutSignal, signal_id: str, reject_reason: str) -> None:
        if self._sf is None:
            return
        s = self._sf()
        try:
            StrategySignalRepo(s).save(StrategySignalRow(
                id=signal_id,
                strategy="Breakout_V1",
                coin=signal.coin,
                side=signal.side,
                entry_price=signal.entry_price,
                sl_price=signal.sl_price,
                tp_price=signal.tp_price,
                regime_score=signal.regime_score,
                volume_zscore=signal.volume_zscore,
                oi_change_pct=signal.oi_change_pct,
                reason=signal.reason,
                status="REJECTED",
                reject_reason=reject_reason,
                created_at=int(time.time()),
            ))
        except Exception as exc:
            logger.warning("paper: save rejected signal failed: %s", exc)
        finally:
            s.close()

    async def _close(self, coin: str, exit_price: float, reason: str) -> None:
        pos = self._positions.pop(coin, None)
        if pos is None:
            return

        if pos.side == "LONG":
            pnl = pos.size_notional * (exit_price - pos.entry_price) / pos.entry_price
        else:
            pnl = pos.size_notional * (pos.entry_price - exit_price) / pos.entry_price

        pnl -= pos.fee_usd  # subtract open fee
        exit_fee = pos.size_notional * self._exec.fee_taker_bps / 10000
        pnl -= exit_fee

        self._realized_pnl_today += pnl

        # Update drawdown tracking
        current_equity = self._exec.account_balance + self._realized_pnl_today
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
        drawdown = (self._peak_equity - current_equity) / self._peak_equity * 100
        if drawdown > self._max_drawdown_today:
            self._max_drawdown_today = drawdown

        now = int(time.time())

        # Persist close to DB
        if self._sf is not None:
            s = self._sf()
            try:
                row = PaperPositionRepo(s).get_open(coin)
                if row is not None:
                    row.status = "CLOSED"
                    row.exit_price = exit_price
                    row.exit_reason = reason
                    row.pnl_usd = pnl
                    row.closed_at = now
                    row.fee_usd = (pos.fee_usd or 0.0) + exit_fee
                    s.commit()

                sig = StrategySignalRepo(s).get_active(coin)
                if sig is not None:
                    sig.status = "CLOSED"
                    sig.closed_at = now
                    s.commit()
            except Exception as exc:
                logger.warning("paper: DB close error: %s", exc)
                s.rollback()
            finally:
                s.close()

        pnl_pct = pnl / pos.size_notional * 100
        sign = "+" if pnl >= 0 else ""
        label = {
            "sl": "🛑 Stop Loss",
            "tp": "✅ Take Profit",
            "emergency_stop": "⚠️ Emergency Stop",
        }.get(reason, reason)

        logger.info(
            "paper CLOSE %s %s reason=%s exit=%.4f pnl=%s$%.2f (%s%.1f%%)",
            coin, pos.side, reason, exit_price, sign, pnl, sign, pnl_pct,
        )
        coin_size = pos.size_notional / pos.entry_price if pos.entry_price else 0
        notional_fmt = f"~${pos.size_notional/1000:.0f}k" if pos.size_notional >= 1000 else f"~${pos.size_notional:.0f}"
        await self._notifier.send("info", (
            f"📄 PAPER CLOSE — {label}\n\n"
            f"{coin} {pos.side}\n"
            f"Size: {coin_size:.4g} {coin} ({notional_fmt})\n"
            f"Entry: ${pos.entry_price:,.4f} → Exit: ${exit_price:,.4f}\n"
            f"PnL: {sign}${abs(pnl):.2f} ({sign}{pnl_pct:.1f}%)\n"
            f"Daily PnL: {'+' if self._realized_pnl_today >= 0 else ''}${self._realized_pnl_today:.2f}"
        ))

    def _save_equity_snapshot(self, now: int) -> None:
        unrealized = 0.0
        for coin, pos in self._positions.items():
            price = self._prices.get(coin)
            if price is None:
                continue
            if pos.side == "LONG":
                unrealized += pos.size_notional * (price - pos.entry_price) / pos.entry_price
            else:
                unrealized += pos.size_notional * (pos.entry_price - price) / pos.entry_price

        equity = self._exec.account_balance + self._realized_pnl_today + unrealized
        s = self._sf()
        try:
            EquitySnapshotRepo(s).save(EquitySnapshotRow(
                timestamp=now,
                equity=equity,
                unrealized_pnl=unrealized,
                realized_pnl_today=self._realized_pnl_today,
                max_drawdown_today=self._max_drawdown_today,
                open_positions=len(self._positions),
            ))
        except Exception as exc:
            logger.warning("paper: equity snapshot error: %s", exc)
            s.rollback()
        finally:
            s.close()

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[PaperPosition]:
        return list(self._positions.values())

    def get_daily_pnl(self) -> float:
        return self._realized_pnl_today

    def reset_daily(self) -> None:
        self._realized_pnl_today = 0.0
        self._max_drawdown_today = 0.0
        self._peak_equity = self._exec.account_balance
        logger.info("paper: daily stats reset")
