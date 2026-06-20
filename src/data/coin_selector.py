"""
CoinSelector — dynamically picks the top N perp coins by 24h volume.

Refreshes every `refresh_days` days. Caches result in Redis (primary)
and falls back to in-memory if Redis is unavailable.

Usage:
    selector = CoinSelector(client, redis=redis_client, n=10)
    coins = await selector.get_active_coins()   # cached
    coins = await selector.refresh()            # force refresh
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

_REDIS_KEY = "hl:top_coins"


class CoinSelector:
    def __init__(
        self,
        client,
        redis=None,
        n: int = 10,
        refresh_days: int = 7,
        fallback: list[str] | None = None,
    ) -> None:
        self._client = client
        self._redis = redis
        self._n = n
        self._ttl = refresh_days * 86400
        self._fallback = fallback or ["BTC", "ETH", "SOL", "WIF", "HYPE"]
        self._cached: list[str] | None = None
        self._cached_at: float = 0.0

    async def get_active_coins(self) -> list[str]:
        """Return active coin list, using cache if fresh enough."""
        # 1. In-process memory cache
        if self._cached and time.time() - self._cached_at < self._ttl:
            return self._cached

        # 2. Redis cache
        if self._redis is not None:
            try:
                raw = await self._redis.get(_REDIS_KEY)
                if raw:
                    payload = json.loads(raw)
                    age = time.time() - payload.get("fetched_at", 0)
                    if age < self._ttl:
                        self._cached = payload["coins"]
                        self._cached_at = payload["fetched_at"]
                        logger.info(
                            "coin_selector: loaded %d coins from Redis (age %.0fh)",
                            len(self._cached), age / 3600,
                        )
                        return self._cached
            except Exception as exc:
                logger.warning("coin_selector: redis read failed: %s", exc)

        # 3. Fetch fresh
        return await self.refresh()

    async def refresh(self) -> list[str]:
        """Force-fetch top N coins from Hyperliquid API and update cache."""
        try:
            coins = await self._client.get_top_coins_by_volume(self._n)
            if not coins:
                raise ValueError("empty response")
        except Exception as exc:
            logger.warning("coin_selector: API fetch failed (%s) — using fallback", exc)
            return self._cached or self._fallback

        self._cached = coins
        self._cached_at = time.time()

        if self._redis is not None:
            try:
                await self._redis.setex(
                    _REDIS_KEY,
                    self._ttl + 3600,  # small buffer so Redis TTL > selector TTL
                    json.dumps({"coins": coins, "fetched_at": self._cached_at}),
                )
            except Exception as exc:
                logger.warning("coin_selector: redis write failed: %s", exc)

        logger.info("coin_selector: refreshed top %d = %s", self._n, coins)
        return coins
