from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token, self.chat_id = token, chat_id

    async def send(self, message: str) -> dict[str, object]:
        if not self.token or not self.chat_id:
            logger.info("Telegram unavailable; console alert: %s", message)
            return {"delivered": False, "fallback": "console"}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json={"chat_id": self.chat_id, "text": message})
            response.raise_for_status()
        return {"delivered": True, "fallback": None}


def validate_chat(expected_chat_id: str | None, actual_chat_id: str) -> bool:
    return bool(expected_chat_id) and expected_chat_id == actual_chat_id
