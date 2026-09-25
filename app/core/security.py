import base64
import hashlib
import hmac
import time

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.errors import AppError


class SecretBox:
    def __init__(self, key: str | None = None):
        raw = (key if key is not None else get_settings().encryption_key).encode()
        if not raw:
            raw = b"taskmate-development-key"
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise AppError("AUTH_ERROR", "Credential cannot be decrypted", 401) from exc


def issue_user_token(telegram_user_id: int, expires_in: int = 86400) -> str:
    """Issue an API token bound to a Telegram identity verified by the webhook."""
    expiry = int(time.time()) + expires_in
    payload = f"tm1.{telegram_user_id}.{expiry}"
    signature = hmac.new(
        get_settings().internal_api_token.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_user_token(token: str) -> int:
    try:
        prefix, identity, expiry, signature = token.split(".")
        if prefix != "tm1" or int(expiry) <= int(time.time()):
            raise ValueError
        payload = f"{prefix}.{identity}.{expiry}"
        expected = hmac.new(
            get_settings().internal_api_token.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        return int(identity)
    except (ValueError, AttributeError):
        raise AppError("AUTH_ERROR", "Invalid or expired user token", 401) from None
