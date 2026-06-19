from __future__ import annotations
import logging
import os
from typing import Protocol
import httpx

logger = logging.getLogger("monitoring")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class Notifier(Protocol):
    async def send(self, level: str, message: str) -> None: ...


class LogNotifier:
    async def send(self, level: str, message: str) -> None:
        logger.log(_LEVELS.get(level, logging.INFO), message)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def send(self, level: str, message: str) -> None:
        url = TELEGRAM_API_URL.format(token=self._token)
        try:
            resp = await self._client.post(
                url, json={"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Telegram alert failed: %s", exc)


class TaggedNotifier:
    """Prepends a fixed bot tag to every outgoing message."""
    def __init__(self, inner: Notifier, tag: str) -> None:
        self._inner = inner
        self._tag = tag

    async def send(self, level: str, message: str) -> None:
        await self._inner.send(level, f"<b>{self._tag}</b>\n{message}")


def build_notifier(token: str | None = None, chat_id: str | None = None) -> Notifier:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    return LogNotifier()
