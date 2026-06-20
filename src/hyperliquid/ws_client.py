from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets

logger = logging.getLogger("hyperliquid.ws")

CandleCallback = Callable[[str, str, dict], Awaitable[None]]   # (coin, interval, candle_data)
TradesCallback = Callable[[str, list[dict]], Awaitable[None]]   # (coin, trades_list)
PriceCallback = Callable[[dict[str, float]], Awaitable[None]]   # {symbol: mid_price}
ReconnectCallback = Callable[[], Awaitable[None]]


class MarketWSClient:
    """Single-connection WebSocket client for Hyperliquid market data streams."""

    def __init__(
        self,
        ws_url: str = "wss://api.hyperliquid.xyz/ws",
        reconnect_delay_s: int = 5,
        ping_interval_s: int = 30,
    ) -> None:
        self._ws_url = ws_url
        self._reconnect_delay_s = reconnect_delay_s
        self._ping_interval_s = ping_interval_s

        # (coin, interval) -> callback
        self._candle_subs: dict[tuple[str, str], CandleCallback] = {}
        # coin -> callback
        self._trades_subs: dict[str, TradesCallback] = {}
        self._price_callback: PriceCallback | None = None
        self._reconnect_callback: ReconnectCallback | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe_candle(self, coin: str, interval: str, callback: CandleCallback) -> None:
        """Register coin+interval for candle streaming. Call before run()."""
        self._candle_subs[(coin, interval)] = callback

    def subscribe_trades(self, coin: str, callback: TradesCallback) -> None:
        """Register coin for trades streaming."""
        self._trades_subs[coin] = callback

    def set_price_callback(self, callback: PriceCallback) -> None:
        """Set callback for allMids price updates."""
        self._price_callback = callback

    def set_reconnect_callback(self, callback: ReconnectCallback) -> None:
        """Called after each successful reconnect."""
        self._reconnect_callback = callback

    async def run(self, stop: asyncio.Event) -> None:
        """Main loop: connect → subscribe all → handle messages → auto-reconnect.

        Never raises. Runs until stop is set.
        """
        attempt = 0
        while not stop.is_set():
            try:
                logger.info("ws: connecting to %s (attempt %d)", self._ws_url, attempt + 1)
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=None,
                    ping_timeout=None,
                ) as ws:
                    attempt = 0
                    logger.info(
                        "ws: connected — subscribing %d candles, %d trades",
                        len(self._candle_subs),
                        len(self._trades_subs),
                    )
                    await self._subscribe_all(ws)
                    logger.info("ws: subscriptions sent, listening for events")
                    if self._reconnect_callback is not None:
                        try:
                            await self._reconnect_callback()
                        except Exception as exc:
                            logger.warning("ws: reconnect_callback error: %s", exc)
                    await self._handle_loop(ws, stop)
            except Exception as exc:
                if stop.is_set():
                    break
                attempt += 1
                logger.warning(
                    "ws disconnected: %s — reconnect in %ds (attempt %d)",
                    exc,
                    self._reconnect_delay_s,
                    attempt,
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._reconnect_delay_s)
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _subscribe_all(self, ws) -> None:
        """Send subscription messages for all registered candles + trades + allMids."""
        for coin, interval in self._candle_subs:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "candle", "coin": coin, "interval": interval},
            }))

        for coin in self._trades_subs:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin},
            }))

        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "allMids"},
        }))

    async def _handle_loop(self, ws, stop: asyncio.Event) -> None:
        """Receive messages in a loop; run ping task in parallel."""
        ping_task = asyncio.create_task(self._ping_loop(ws, stop))
        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await self._dispatch(raw)
                except Exception as exc:
                    logger.warning("ws: dispatch error (message skipped): %s", exc)
        finally:
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)

    async def _ping_loop(self, ws, stop: asyncio.Event) -> None:
        """Send a ping every ping_interval_s seconds."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._ping_interval_s)
            except asyncio.TimeoutError:
                await ws.send("ping")

    async def _dispatch(self, raw: str) -> None:
        """Parse a raw WebSocket message and invoke the appropriate callback."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug("non-JSON message received: %r", raw)
            return

        channel = msg.get("channel")

        if channel == "candle":
            data = msg.get("data", {})
            coin: str = data.get("s", "")
            interval: str = data.get("i", "")
            cb = self._candle_subs.get((coin, interval))
            if cb is not None:
                # HL WS does not send "x" (closed flag); closure detected via open_time
                # transition in the subscriber callback.
                candle = {
                    "open_time": int(data["t"]) // 1000,
                    "close_time": int(data["T"]) // 1000,
                    "open": float(data["o"]),
                    "high": float(data["h"]),
                    "low": float(data["l"]),
                    "close": float(data["c"]),
                    "volume": float(data["v"]),
                    "num_trades": int(data.get("n", 0)),
                }
                await cb(coin, interval, candle)
            else:
                logger.debug("ws: unregistered candle %s/%s — skipping", coin, interval)

        elif channel == "trades":
            data = msg.get("data", [])
            if not data:
                return
            coin = data[0].get("coin", "")
            cb_trades = self._trades_subs.get(coin)
            if cb_trades is not None:
                await cb_trades(coin, data)
            else:
                logger.debug("ws: unregistered trades for %s — skipping", coin)

        elif channel == "allMids":
            data = msg.get("data", {})
            raw_mids: dict = data.get("mids", {})
            mids: dict[str, float] = {}
            for symbol, price_str in raw_mids.items():
                try:
                    mids[symbol] = float(price_str)
                except (ValueError, TypeError):
                    logger.debug("could not parse mid price for %s: %r", symbol, price_str)
            if self._price_callback is not None:
                await self._price_callback(mids)

        elif channel in ("pong", "subscriptionResponse"):
            pass

        else:
            logger.debug("unhandled channel: %r", channel)
