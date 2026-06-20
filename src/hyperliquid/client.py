from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_BACKOFF_DELAYS = (1.0, 2.0, 4.0)

_CANDLES_CACHE_TTL_S = 3600
_FUNDING_CACHE_TTL_S = 3600


class HyperliquidAPIUnavailable(RuntimeError):
    """Raised when Hyperliquid does not return trustworthy data after retries."""


class HyperliquidClient:
    """Async REST client for the Hyperliquid public info API."""

    def __init__(
        self,
        base_url: str = "https://api.hyperliquid.xyz/info",
        timeout: float = 20.0,
        redis_url: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._redis_url = redis_url
        self._redis = None  # lazy-init on first cache access

    # ------------------------------------------------------------------
    # Redis cache helpers
    # ------------------------------------------------------------------

    async def _redis_get(self, key: str) -> Any | None:
        try:
            r = await self._get_redis()
            if r is None:
                return None
            raw = await r.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.debug("redis get failed (%s): %s", key, exc)
            return None

    async def _redis_set(self, key: str, data: Any, ttl_s: int = _CANDLES_CACHE_TTL_S) -> None:
        try:
            r = await self._get_redis()
            if r is None:
                return
            await r.setex(key, ttl_s, json.dumps(data))
        except Exception as exc:
            logger.debug("redis set failed (%s): %s", key, exc)

    async def _get_redis(self):
        if self._redis is None and self._redis_url:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                await self._redis.ping()
                logger.debug("redis: connected for cache")
            except Exception as exc:
                logger.warning("redis: unavailable, caching disabled (%s)", exc)
                self._redis_url = None  # don't retry
                self._redis = None
        return self._redis

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post(self, payload: dict) -> Any:
        """POST *payload* to the info endpoint with retry on 429/5xx."""
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_BACKOFF_DELAYS, None), start=0):
            try:
                response = await self._client.post(self._base_url, json=payload)

                if response.status_code == 404:
                    logger.warning(
                        "Hyperliquid 404 for payload type=%s", payload.get("type")
                    )
                    return None

                if response.status_code in _RETRY_STATUSES and delay is not None:
                    logger.warning(
                        "Hyperliquid HTTP %s (attempt %s), retrying in %.0fs …",
                        response.status_code,
                        attempt + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if response.status_code in _RETRY_STATUSES:
                    logger.warning(
                        "Hyperliquid HTTP %s after %s attempts, giving up",
                        response.status_code,
                        attempt + 1,
                    )
                    raise HyperliquidAPIUnavailable(
                        f"Hyperliquid HTTP {response.status_code} after {attempt + 1} attempts"
                    )

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as exc:
                logger.warning("Hyperliquid HTTP error: %s", exc)
                last_exc = exc
                break
            except httpx.RequestError as exc:
                logger.warning(
                    "Hyperliquid request error (attempt %s): %s", attempt + 1, exc
                )
                last_exc = exc
                if delay is not None:
                    await asyncio.sleep(delay)
                continue

        if last_exc is not None:
            raise last_exc
        return None

    async def _get(self, url: str) -> Any:
        """GET request with retry on 429/5xx."""
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_BACKOFF_DELAYS, None), start=0):
            try:
                response = await self._client.get(url)
                if response.status_code in _RETRY_STATUSES and delay is not None:
                    logger.warning(
                        "Hyperliquid HTTP %s (attempt %s), retrying in %.0fs …",
                        response.status_code, attempt + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code in _RETRY_STATUSES:
                    logger.warning(
                        "Hyperliquid HTTP %s after %s attempts, giving up",
                        response.status_code, attempt + 1,
                    )
                    return None
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                logger.warning("Hyperliquid HTTP error: %s", exc)
                last_exc = exc
                break
            except httpx.RequestError as exc:
                logger.warning("Hyperliquid request error (attempt %s): %s", attempt + 1, exc)
                last_exc = exc
                if delay is not None:
                    await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_all_mids(self) -> dict[str, float]:
        """Return current mid prices keyed by coin symbol."""
        data = await self._post({"type": "allMids"})
        if not data:
            return {}
        return {coin: float(price) for coin, price in data.items()}

    async def get_meta(self) -> dict:
        """Return raw coin metadata from the /info endpoint."""
        data = await self._post({"type": "meta"})
        return data if isinstance(data, dict) else {}

    async def get_candles(
        self,
        coin: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int | None = None,
    ) -> list[dict]:
        """Fetch historical candles."""
        start_day = start_time_ms // 86_400_000
        cache_key = f"hl:candles:{coin}:{interval}:{start_day}"

        if end_time_ms is None:
            cached = await self._redis_get(cache_key)
            if cached is not None:
                return cached

        req: dict[str, Any] = {"coin": coin, "interval": interval, "startTime": start_time_ms}
        if end_time_ms is not None:
            req["endTime"] = end_time_ms

        data = await self._post({"type": "candleSnapshot", "req": req})
        if not data:
            return []

        candles = [
            {
                "open_time": int(c["t"]) // 1000,
                "close_time": int(c["T"]) // 1000,
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low": float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
            }
            for c in data
        ]

        if end_time_ms is None:
            await self._redis_set(cache_key, candles, _CANDLES_CACHE_TTL_S)

        return candles

    async def get_funding_history(
        self,
        coin: str,
        start_time_ms: int,
    ) -> list[dict]:
        """Fetch historical funding rates."""
        start_day = start_time_ms // 86_400_000
        cache_key = f"hl:funding:{coin}:{start_day}"

        cached = await self._redis_get(cache_key)
        if cached is not None:
            return cached

        data = await self._post({"type": "fundingHistory", "coin": coin, "startTime": start_time_ms})
        if not data:
            return []

        rates = [
            {
                "coin": entry.get("coin", coin),
                "funding_time": int(entry["time"]) // 1000,
                "rate": float(entry["fundingRate"]),
            }
            for entry in data
        ]

        await self._redis_set(cache_key, rates, _FUNDING_CACHE_TTL_S)
        return rates

    async def get_top_coins_by_volume(
        self,
        n: int = 10,
        exclude: set[str] | None = None,
    ) -> list[str]:
        """Return top N perp coins ranked by 24h notional volume."""
        _STABLECOINS = {"USDC", "USDT", "DAI", "BUSD", "TUSD", "USDE", "FDUSD"}
        excluded = (exclude or set()) | _STABLECOINS

        data = await self._post({"type": "metaAndAssetCtxs"})
        if not data or len(data) < 2:
            return []

        universe: list[dict] = data[0].get("universe", [])
        asset_ctxs: list[dict] = data[1]

        pairs = [
            (coin_meta.get("name", ""), float(ctx.get("dayNtlVlm", 0) or 0))
            for coin_meta, ctx in zip(universe, asset_ctxs)
            if coin_meta.get("name", "") not in excluded
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in pairs[:n]]

    async def get_asset_contexts(self) -> list[dict]:
        """Fetch current OI and mark price for all coins (no cache)."""
        data = await self._post({"type": "metaAndAssetCtxs"})
        if not data or len(data) < 2:
            return []

        universe: list[dict] = data[0].get("universe", [])
        asset_ctxs: list[dict] = data[1]

        results = []
        for coin_meta, ctx in zip(universe, asset_ctxs):
            oi = float(ctx.get("openInterest", 0) or 0)
            if oi <= 0:
                continue
            results.append(
                {
                    "coin": coin_meta.get("name", ""),
                    "mark_price": float(ctx.get("markPx", 0) or 0),
                    "open_interest": oi,
                }
            )

        return results

    async def get_recent_trades(self, coin: str) -> list[dict]:
        """Fetch recent trades for a coin (no cache)."""
        data = await self._post({"type": "recentTrades", "coin": coin})
        if not data:
            return []
        return data

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
        if self._redis is not None:
            await self._redis.aclose()

    async def __aenter__(self) -> "HyperliquidClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()
