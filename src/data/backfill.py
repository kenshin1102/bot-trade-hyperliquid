from __future__ import annotations

"""
Backfill historical candles, funding rates, and OI from Hyperliquid REST.

CLI:
    python -m src.data.backfill                     # run once
    python -m src.data.backfill --loop --hours 1    # update every hour
    python -m src.data.backfill --days 30           # only backfill last 30 days
"""

import argparse
import asyncio
import logging
import time

from src.config.settings import load_config, load_secrets
from src.data.coin_selector import CoinSelector
from src.hyperliquid.client import HyperliquidClient
from src.monitoring.notifier import TaggedNotifier, build_notifier
from src.storage.db import AssetContextRow, CandleRow, FundingRateRow, init_db, make_session_factory
from src.storage.repository import AssetContextRepo, CandleRepo, FundingRateRepo

logger = logging.getLogger("data.backfill")


_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480, "12h": 720, "1d": 1440,
}
_MAX_CANDLES_PER_REQUEST = 5000


async def backfill_candles(client: HyperliquidClient, repo: CandleRepo, coins: list[str], intervals: list[str], days: int) -> None:
    now = time.time()
    for coin in coins:
        for interval in intervals:
            try:
                start_ms = int((now - days * 86400) * 1000)
                end_ms = int(now * 1000)
                interval_min = _INTERVAL_MINUTES.get(interval, 60)
                # Max window per API call to stay within ~5000 candle limit
                chunk_ms = _MAX_CANDLES_PER_REQUEST * interval_min * 60 * 1000

                all_rows: list[CandleRow] = []
                chunk_start = start_ms
                while chunk_start < end_ms:
                    chunk_end = min(chunk_start + chunk_ms, end_ms)
                    raw = await client.get_candles(coin, interval, chunk_start, chunk_end)
                    if raw:
                        all_rows.extend(
                            CandleRow(
                                id=f"{coin}:{interval}:{c['open_time']}",
                                coin=coin,
                                interval=interval,
                                open_time=c["open_time"],
                                close_time=c["close_time"],
                                open=c["open"],
                                high=c["high"],
                                low=c["low"],
                                close=c["close"],
                                volume=c["volume"],
                                created_at=int(now),
                            )
                            for c in raw
                        )
                    chunk_start = chunk_end + 1
                    await asyncio.sleep(0.3)

                repo.upsert_many(all_rows)
                logger.info("backfill: %s %s → %d candles", coin, interval, len(all_rows))
            except Exception as exc:
                logger.error("backfill: candles %s %s failed: %s", coin, interval, exc)
            await asyncio.sleep(0.3)


async def backfill_funding(client: HyperliquidClient, repo: FundingRateRepo, coins: list[str], days: int) -> None:
    now = time.time()
    for coin in coins:
        try:
            start_time_ms = int((now - days * 86400) * 1000)
            raw = await client.get_funding_history(coin, start_time_ms)
            rows = [
                FundingRateRow(
                    id=f"{coin}:{r['funding_time']}",
                    coin=r.get("coin", coin),
                    funding_time=r["funding_time"],
                    rate=r["rate"],
                    created_at=int(now),
                )
                for r in raw
            ]
            repo.upsert_many(rows)
            logger.info("backfill: %s funding → %d rates", coin, len(rows))
        except Exception as exc:
            logger.error("backfill: funding %s failed: %s", coin, exc)
        await asyncio.sleep(0.3)


async def backfill_asset_contexts(client: HyperliquidClient, repo: AssetContextRepo, coins: list[str]) -> None:
    timestamp_s = int(time.time())
    try:
        all_ctxs = await client.get_asset_contexts()
        coin_set = set(coins)
        count = 0
        for ctx in all_ctxs:
            coin = ctx.get("coin", "")
            if coin not in coin_set:
                continue
            row = AssetContextRow(
                id=f"{coin}:{timestamp_s}",
                coin=coin,
                timestamp=timestamp_s,
                mark_price=ctx["mark_price"],
                open_interest=ctx["open_interest"],
                created_at=timestamp_s,
            )
            repo.upsert(row)
            count += 1
        logger.info("backfill: asset contexts → %d coins", count)
    except Exception as exc:
        logger.error("backfill: asset_contexts failed: %s", exc)


async def run_once(client: HyperliquidClient, session_factory, cfg, notifier) -> None:
    t0 = time.time()
    session = session_factory()
    try:
        candle_repo = CandleRepo(session)
        funding_repo = FundingRateRepo(session)
        ctx_repo = AssetContextRepo(session)

        selector = CoinSelector(
            client, redis=None,
            n=cfg.data.top_coins_n,
            refresh_days=cfg.data.top_coins_refresh_days,
            fallback=cfg.data.coins,
        )
        coins: list[str] = await selector.get_active_coins()
        intervals: list[str] = cfg.data.candle_intervals
        days: int = cfg.data.backfill_days

        logger.info("backfill: active coins = %s", coins)
        await backfill_candles(client, candle_repo, coins, intervals, days)
        await backfill_funding(client, funding_repo, coins, days)
        await backfill_asset_contexts(client, ctx_repo, coins)

        elapsed = time.time() - t0
        logger.info("backfill: done in %.1fs", elapsed)
    except Exception as exc:
        logger.error("backfill: run_once error: %s", exc)
        await notifier.send("error", f"Backfill error: {exc}")
    finally:
        session.close()


async def run(loop: bool, hours: float, days: int | None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()
    secrets = load_secrets()

    if days is not None:
        cfg.data.backfill_days = days

    notifier = TaggedNotifier(build_notifier(secrets.telegram_bot_token, secrets.telegram_chat_id), "HL-STRAT")
    init_db(secrets.database_url)
    sf = make_session_factory(secrets.database_url)

    async with HyperliquidClient(redis_url=secrets.redis_url) as client:
        while True:
            await run_once(client, sf, cfg, notifier)
            await notifier.send("info", "✅ Backfill done")
            if not loop:
                break
            logger.info("backfill: sleeping %.1f hours until next run", hours)
            await asyncio.sleep(hours * 3600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Hyperliquid historical data")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--hours", type=float, default=1.0, help="Hours between runs (default: 1)")
    parser.add_argument("--days", type=int, default=None, help="Days of history to backfill (default: from config)")
    args = parser.parse_args()

    asyncio.run(run(loop=args.loop, hours=args.hours, days=args.days))


if __name__ == "__main__":
    main()
