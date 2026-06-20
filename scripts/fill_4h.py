"""One-shot: fill all missing 1h/4h candles for the 30-coin pool."""
import asyncio, sys, time
sys.path.insert(0, ".")
from src.config.settings import load_secrets
from src.hyperliquid.client import HyperliquidClient
from src.storage.db import CandleRow, init_db, make_session_factory
from src.storage.repository import CandleRepo

COINS = [
    "BTC","HYPE","ETH","SOL","ZEC","WLD","NEAR","XPL","EIGEN","AVAX",
    "XRP","LIT","JTO","UNI","XMR","TAO","AAVE","MET","AERO","SUI",
    "VVV","CRV","PUMP","ONDO","ASTER","ENA","FARTCOIN","XLM","kPEPE","BNB",
]
INTERVALS = [("1h", 3_600_000), ("4h", 14_400_000)]
DAYS = 180
CHUNK = 5000


async def main() -> None:
    secrets = load_secrets()
    init_db(secrets.database_url)
    session = make_session_factory(secrets.database_url)()
    repo = CandleRepo(session)
    now = time.time()
    full_start_ms = int((now - DAYS * 86400) * 1000)
    end_ms = int(now * 1000)

    async with HyperliquidClient(redis_url=secrets.redis_url) as client:
        for interval, interval_ms in INTERVALS:
            chunk_ms = CHUNK * interval_ms
            for coin in COINS:
                oldest = repo.get_oldest(coin, interval, 1)
                latest = repo.get_latest(coin, interval, 1)
                oldest_ts = oldest[0].open_time * 1000 if oldest else None
                latest_ts = latest[0].open_time * 1000 if latest else None

                async def fetch(start: int, end: int) -> list[CandleRow]:
                    rows: list[CandleRow] = []
                    cs = start
                    while cs < end:
                        ce = min(cs + chunk_ms, end)
                        raw = await client.get_candles(coin, interval, cs, ce)
                        if raw:
                            rows.extend(
                                CandleRow(
                                    id=f"{coin}:{interval}:{c['open_time']}",
                                    coin=coin, interval=interval,
                                    open_time=c["open_time"], close_time=c["close_time"],
                                    open=c["open"], high=c["high"], low=c["low"],
                                    close=c["close"], volume=c["volume"],
                                    created_at=int(now),
                                )
                                for c in raw
                            )
                        cs = ce + 1
                        await asyncio.sleep(0.2)
                    return rows

                # Backward gap
                if oldest_ts and oldest_ts > full_start_ms + interval_ms:
                    rows = await fetch(full_start_ms, oldest_ts - interval_ms)
                    if rows:
                        repo.upsert_many(rows)
                        print(f"BACKWARD {coin} {interval}: +{len(rows)}")

                # Forward gap
                fwd_start = (latest_ts + interval_ms) if latest_ts else full_start_ms
                if fwd_start < end_ms:
                    rows = await fetch(fwd_start, end_ms)
                    if rows:
                        repo.upsert_many(rows)
                        print(f"FORWARD  {coin} {interval}: +{len(rows)}")

    session.close()
    print("ALL DONE")


asyncio.run(main())
