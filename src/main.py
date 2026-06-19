"""
Main entry point: WS streaming + strategy evaluation loop + daily report.

CLI:
    python -m src.main
    python -m src.main --report-hours 24
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal as _signal
import sys
import time
from pathlib import Path

from src.config.settings import load_config, load_secrets
from src.execution.paper import PaperEngine
from src.hyperliquid.ws_client import MarketWSClient
from src.monitoring.log_setup import setup as setup_log
from src.monitoring.notifier import TaggedNotifier, build_notifier
from src.signal.daily_report import send_daily_report
from src.storage.db import CandleRow, init_db, make_session_factory
from src.storage.repository import CandleRepo
from src.strategy.breakout import BreakoutV1
from src.strategy.feature_engine import FeatureEngine
from src.strategy.regime import RegimeDetector

logger = logging.getLogger("main")

_LOCK_FILE = Path("/tmp/hl_strategy_bot.lock")


def _acquire_lock() -> None:
    """Exit immediately if another strategy bot instance is already running."""
    if _LOCK_FILE.exists():
        pid = _LOCK_FILE.read_text().strip()
        try:
            os.kill(int(pid), 0)
            print(f"ERROR: Another instance is running (PID {pid}). Exiting.", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass
    _LOCK_FILE.write_text(str(os.getpid()))


def _release_lock() -> None:
    _LOCK_FILE.unlink(missing_ok=True)


async def run(report_hours: float = 24.0) -> None:
    cfg = load_config()
    secrets = load_secrets()

    if cfg.risk.emergency_stop:
        logger.error("EMERGENCY STOP — abort")
        return

    notifier = TaggedNotifier(build_notifier(secrets.telegram_bot_token, secrets.telegram_chat_id), "HL-STRAT")

    # DB
    init_db(secrets.database_url)
    sf = make_session_factory(secrets.database_url)

    # Strategy components
    feature_engine = FeatureEngine(sf)
    regime_detector = RegimeDetector(cfg.regime)
    strategy = BreakoutV1(cfg.strategy, cfg.risk, regime_detector)
    paper = PaperEngine(cfg.risk, cfg.execution, notifier, sf)

    # WS
    ws = MarketWSClient(reconnect_delay_s=5)

    # Price callback → paper engine SL/TP check
    ws.set_price_callback(paper.on_price_update)

    # Candle callback → save to DB → evaluate strategy
    async def on_candle(coin: str, interval: str, candle: dict) -> None:
        if not candle.get("is_closed", False):
            return  # only process closed candles

        # Persist closed candle so feature engine always has fresh data
        open_time_s = candle["open_time"]
        s = sf()
        try:
            CandleRepo(s).upsert(CandleRow(
                id=f"{coin}:{interval}:{open_time_s}",
                coin=coin,
                interval=interval,
                open_time=open_time_s,
                close_time=candle["close_time"],
                open=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
                volume=candle["volume"],
                created_at=int(time.time()),
            ))
        except Exception as exc:
            logger.warning("save candle failed %s %s: %s", coin, interval, exc)
        finally:
            s.close()

        if interval != cfg.strategy.timeframe:
            return  # saved to DB but don't evaluate on 1h candles

        features = feature_engine.compute(coin, interval)
        if features is None:
            return

        btc_features = feature_engine.compute("BTC", interval) if coin != "BTC" else None

        # Get recent candles from DB for range calculation
        s = sf()
        try:
            candles = CandleRepo(s).get_latest(coin, interval, cfg.strategy.breakout_lookback_candles + 2)
            candles = list(reversed(candles))  # ascending
        finally:
            s.close()

        current_price = paper._prices.get(coin)
        if current_price is None:
            return

        # Estimate spread from price (very rough; replace with orderbook data later)
        spread_bps = 5.0

        signal = strategy.evaluate(coin, features, btc_features, candles, current_price, spread_bps)
        if signal is None:
            return

        signal_id = f"Breakout_V1:{coin}:{int(time.time())}"
        await paper.on_signal(signal, signal_id)

    # Subscribe market data
    for coin in cfg.data.coins:
        ws.subscribe_candle(coin, cfg.strategy.timeframe, on_candle)
        ws.subscribe_candle(coin, "1h", on_candle)  # 1h for regime trend

    stop = asyncio.Event()
    _signal.signal(_signal.SIGINT, lambda *_: stop.set())
    _signal.signal(_signal.SIGTERM, lambda *_: stop.set())

    # Daily report loop
    async def daily_report_loop() -> None:
        interval = report_hours * 3600
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await send_daily_report(notifier, paper, sf, cfg.execution)
                except Exception as exc:
                    logger.warning("daily report failed: %s", exc)

    n = len(cfg.data.coins)
    logger.info("Starting strategy bot | %d coins | mode=%s", n, cfg.execution.mode)
    await notifier.send("info", f"🤖 Strategy bot started | {n} coins | {cfg.execution.mode}")

    report_task = asyncio.create_task(daily_report_loop()) if report_hours > 0 else None
    try:
        await ws.run(stop)
    finally:
        stop.set()
        if report_task:
            await asyncio.gather(report_task, return_exceptions=True)
        await notifier.send("info", "🛑 Strategy bot stopped")


def main() -> None:
    setup_log()
    _acquire_lock()
    try:
        p = argparse.ArgumentParser(prog="python -m src.main")
        p.add_argument("--report-hours", type=float, default=24.0,
                       help="Interval between daily reports (hours); <=0 disables")
        args = p.parse_args()
        asyncio.run(run(report_hours=args.report_hours))
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
