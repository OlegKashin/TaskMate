import os

import httpx

from app.core.config import get_settings
from app.core.errors import AppError


class TelegramClient:
    def __init__(self, token: str | None = None):
        if token is not None:
            self.token = token
        elif os.environ.get("PYTEST_CURRENT_TEST"):
            self.token = ""
        else:
            self.token = get_settings().telegram_bot_token
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def _call(self, method: str, payload: dict, timeout: float = 15.0):
        if not self.token:
            return None
        response = httpx.post(f"{self.base_url}/{method}", json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        return body.get("result") if body.get("ok") else None

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def edit_message(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("editMessageText", payload)

    def answer_callback(self, callback_id: str | None, text: str = ""):
        if not callback_id:
            return None
        return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_chat_member(self, chat_id: int, user_id: int):
        return self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def download_file(self, file_id: str, max_bytes: int) -> bytes:
        info = self._call("getFile", {"file_id": file_id})
        if not info or not info.get("file_path"):
            raise AppError("TELEGRAM_FILE_ERROR", "Telegram file unavailable", 502)
        chunks = bytearray()
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        try:
            with httpx.stream("GET", url, timeout=30) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise AppError("VOICE_TOO_LARGE", "Voice message is too large", 413)
        except httpx.HTTPError as exc:
            raise AppError("TELEGRAM_FILE_ERROR", "Telegram file download failed", 502) from exc
        return bytes(chunks)

    def get_updates(
        self, offset: int | None = None, timeout: int = 10, limit: int = 100
    ) -> list[dict]:
        payload: dict = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        try:
            res = self._call("getUpdates", payload, timeout=float(timeout) + 5.0)
            return res if isinstance(res, list) else []
        except httpx.TimeoutException:
            return []

    def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        res = self._call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})
        return bool(res)

    def set_webhook(self, url: str, secret_token: str | None = None) -> bool:
        payload: dict = {
            "url": url,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if secret_token:
            payload["secret_token"] = secret_token
        res = self._call("setWebhook", payload)
        return bool(res)


def main_menu(tokens: dict[str, str]):
    return {
        "inline_keyboard": [
            [
                {"text": "📅 План на сегодня", "callback_data": tokens["today"]},
                {"text": "📥 Предложения AI", "callback_data": tokens["inbox"]},
            ],
            [
                {"text": "📋 Задачи и проекты", "callback_data": tokens["tasks"]},
                {"text": "📆 Расписание встреч", "callback_data": tokens["schedule"]},
            ],
            [
                {"text": "⚙️ Настройки и интеграции", "callback_data": tokens["settings"]},
            ],
        ]
    }

