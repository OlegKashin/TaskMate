"""Browser OAuth with one-use state and provider-verified account identity."""

import imaplib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, conflict
from app.core.security import SecretBox
from app.integrations.telegram.client import TelegramClient
from app.models.entities import (
    CalendarConnection,
    Notification,
    OAuthState,
    Source,
    SourceCredential,
    SourceFolder,
    User,
    utcnow,
)

PROVIDERS = {
    "google": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
    ),
    "gmail": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
    ),
    "yandex": ("https://oauth.yandex.com/authorize", "https://oauth.yandex.com/token"),
    "mailru": ("https://o2.mail.ru/login", "https://o2.mail.ru/token"),
}
SCOPES = {
    "google": "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly",
    "gmail": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send",
    "yandex": "mail:imap_full mail:smtp login:email",
    "mailru": "mail.imap",
}


class OAuthService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _client_credentials(provider: str) -> tuple[str, str]:
        settings = get_settings()
        client_id = getattr(settings, f"{provider}_client_id", "")
        client_secret = getattr(settings, f"{provider}_client_secret", "")
        if provider == "gmail":
            client_id = client_id or settings.google_client_id
            client_secret = client_secret or settings.google_client_secret
        if not client_id or not client_secret:
            raise AppError("OAUTH_NOT_CONFIGURED", f"{provider} OAuth is not configured", 503)
        return client_id, client_secret

    def create_state(
        self, user: User, provider: str, account_email: str | None = None
    ) -> OAuthState:
        if provider not in PROVIDERS:
            raise AppError("VALIDATION_ERROR", "Unsupported OAuth provider", 422)
        self._client_credentials(provider)
        if provider == "mailru" and (not account_email or "@" not in account_email):
            raise AppError("VALIDATION_ERROR", "Mail.ru mailbox email is required", 422)
        settings = get_settings()
        now = utcnow()
        draft = None
        if provider == "google":
            draft = CalendarConnection(user_id=user.id, provider="google", status="connecting")
        elif provider == "mailru":
            draft = self.db.scalar(
                select(Source).where(
                    Source.user_id == user.id,
                    Source.type == provider,
                    Source.external_source_id == account_email,
                )
            )
            if draft:
                draft.status = "connecting"
            else:
                draft = Source(
                    user_id=user.id,
                    type=provider,
                    status="connecting",
                    name=account_email,
                    external_source_id=account_email,
                )
        else:
            draft = Source(
                user_id=user.id,
                type=provider,
                status="connecting",
                name=account_email or provider.title(),
                external_source_id=account_email,
            )
        self.db.add(draft)
        self.db.flush()
        state = OAuthState(
            user_id=user.id,
            provider=provider,
            purpose="calendar" if provider == "google" else "source",
            source_id=draft.id if provider != "google" else None,
            calendar_connection_id=draft.id if provider == "google" else None,
            state_token=secrets.token_urlsafe(32),
            redirect_uri=f"{settings.public_base_url}/api/v1/oauth/{provider}/callback",
            expires_at=now + timedelta(seconds=settings.oauth_state_ttl_seconds),
        )
        self.db.add(state)
        self.db.commit()
        return state

    def authorize_url(self, provider: str, state_token: str) -> str:
        if provider not in PROVIDERS:
            raise AppError("VALIDATION_ERROR", "Unsupported OAuth provider", 422)
        state = self.db.scalar(
            select(OAuthState).where(
                OAuthState.provider == provider,
                OAuthState.state_token == state_token,
                OAuthState.consumed_at.is_(None),
                OAuthState.expires_at > utcnow(),
            )
        )
        if not state:
            raise AppError("AUTH_ERROR", "Invalid or expired OAuth state", 401)
        client_id, _ = self._client_credentials(provider)
        params = {
            "client_id": client_id,
            "redirect_uri": state.redirect_uri,
            "response_type": "code",
            "state": state.state_token,
        }
        if SCOPES[provider]:
            params["scope"] = SCOPES[provider]
        if provider in {"google", "gmail"}:
            params["access_type"] = "offline"
            params["prompt"] = "consent"
        return f"{PROVIDERS[provider][0]}?{urlencode(params)}"

    def _exchange(self, provider: str, code: str, redirect_uri: str) -> dict:
        client_id, client_secret = self._client_credentials(provider)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if provider in {"google", "gmail", "mailru"}:
            form["redirect_uri"] = redirect_uri
        try:
            response = httpx.post(PROVIDERS[provider][1], data=form, timeout=15)
            response.raise_for_status()
            tokens = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("OAUTH_EXCHANGE_FAILED", "Token exchange failed", 502) from exc
        if not tokens.get("access_token"):
            raise AppError("OAUTH_EXCHANGE_FAILED", "Provider did not issue an access token", 502)
        return tokens

    def _account_id(self, provider: str, access_token: str) -> str:
        urls = {
            "gmail": "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            "google": "https://www.googleapis.com/calendar/v3/calendars/primary",
            "yandex": "https://login.yandex.ru/info",
        }
        try:
            response = httpx.get(
                urls[provider],
                headers={
                    "Authorization": f"{'OAuth' if provider == 'yandex' else 'Bearer'} {access_token}"
                },
                timeout=15,
            )
            response.raise_for_status()
            profile = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("OAUTH_PROFILE_FAILED", "Cannot verify provider account", 502) from exc
        account_id = profile.get(
            "emailAddress"
            if provider == "gmail"
            else ("default_email" if provider == "yandex" else "id")
        )
        if not account_id:
            raise AppError("OAUTH_PROFILE_FAILED", "Provider did not identify the account", 502)
        return str(account_id)

    def complete(self, provider: str, state_token: str, code: str):
        if provider not in PROVIDERS or not code:
            raise AppError("VALIDATION_ERROR", "Invalid OAuth callback", 422)
        now = utcnow()
        state = self.db.scalar(
            select(OAuthState).where(
                OAuthState.provider == provider,
                OAuthState.state_token == state_token,
                OAuthState.consumed_at.is_(None),
                OAuthState.expires_at > now,
            )
        )
        if not state:
            raise AppError("AUTH_ERROR", "Invalid or consumed OAuth state", 401)
        changed = self.db.execute(
            update(OAuthState)
            .where(OAuthState.id == state.id, OAuthState.consumed_at.is_(None))
            .values(consumed_at=now)
        )
        if changed.rowcount != 1:
            self.db.rollback()
            raise conflict("OAUTH_STATE_USED", "OAuth state was already consumed")
        # Commit the claim before external I/O so concurrent callbacks cannot reuse the state.
        self.db.commit()
        try:
            tokens = self._exchange(provider, code, state.redirect_uri)
            if provider == "mailru":
                draft = self.db.get(Source, state.source_id)
                account = draft.external_source_id
                try:
                    with imaplib.IMAP4_SSL("imap.mail.ru", 993, timeout=15) as client:
                        client.authenticate(
                            "XOAUTH2",
                            lambda _: (
                                f"user={account}\x01auth=Bearer {tokens['access_token']}\x01\x01"
                            ).encode(),
                        )
                except (imaplib.IMAP4.error, OSError) as exc:
                    raise AppError(
                        "OAUTH_PROFILE_FAILED", "Cannot verify Mail.ru mailbox", 502
                    ) from exc
            else:
                account = self._account_id(provider, tokens["access_token"])
        except AppError:
            draft = self.db.get(
                CalendarConnection if provider == "google" else Source,
                state.calendar_connection_id if provider == "google" else state.source_id,
            )
            if draft:
                draft.status = "error"
                self.db.commit()
            raise
        box = SecretBox()
        expires_at = (
            now + timedelta(seconds=int(tokens["expires_in"])) if tokens.get("expires_in") else None
        )
        if provider == "google":
            connection = self.db.scalar(
                select(CalendarConnection).where(
                    CalendarConnection.user_id == state.user_id,
                    CalendarConnection.provider == "google",
                    CalendarConnection.external_account_id == account,
                )
            )
            if not connection:
                connection = self.db.get(CalendarConnection, state.calendar_connection_id)
            elif connection.id != state.calendar_connection_id:
                draft = self.db.get(CalendarConnection, state.calendar_connection_id)
                state.calendar_connection_id = connection.id
                self.db.delete(draft)
            connection.external_account_id = account
            connection.status = "active"
            connection.encrypted_access_token = box.encrypt(tokens["access_token"])
            if tokens.get("refresh_token"):
                connection.encrypted_refresh_token = box.encrypt(tokens["refresh_token"])
            connection.token_expires_at = expires_at
            result = connection
        else:
            source = self.db.scalar(
                select(Source).where(
                    Source.user_id == state.user_id,
                    Source.type == provider,
                    Source.external_source_id == account,
                )
            )
            if not source:
                source = self.db.get(Source, state.source_id)
            elif source.id != state.source_id:
                draft = self.db.get(Source, state.source_id)
                state.source_id = source.id
                self.db.delete(draft)
            source.name = account
            source.external_source_id = account
            source.status = "active"
            source.connected_at = now
            source.last_analyzed_at = now
            source.last_synced_at = now
            credential = self.db.scalar(
                select(SourceCredential).where(SourceCredential.source_id == source.id)
            )
            if not credential:
                credential = SourceCredential(source_id=source.id, provider=provider)
                self.db.add(credential)
            credential.encrypted_access_token = box.encrypt(tokens["access_token"])
            credential.encrypted_username = box.encrypt(account)
            if tokens.get("refresh_token"):
                credential.encrypted_refresh_token = box.encrypt(tokens["refresh_token"])
            credential.token_expires_at = expires_at
            if not self.db.scalar(
                select(SourceFolder).where(
                    SourceFolder.source_id == source.id, SourceFolder.external_folder_id == "INBOX"
                )
            ):
                self.db.add(
                    SourceFolder(
                        source_id=source.id,
                        external_folder_id="INBOX",
                        name="Входящие",
                        is_selected=True,
                    )
                )
            result = source
        notification = Notification(
            user_id=state.user_id,
            type="oauth_connected",
            payload={
                "title": f"Источник {account} подключён"
                if provider != "google"
                else "Google Calendar подключён"
            },
            dedupe_key=f"oauth-connected:{state.id}",
        )
        self.db.add(notification)
        self.db.commit()
        user = self.db.get(User, state.user_id)
        try:
            if TelegramClient().send_message(user.telegram_user_id, notification.payload["title"]):
                notification.status = "sent"
                notification.sent_at = utcnow()
                self.db.commit()
        except (httpx.HTTPError, OSError):
            # The queued notification is retried by the scheduler.
            pass
        return result

    def fail(self, provider: str, state_token: str) -> None:
        state = self.db.scalar(
            select(OAuthState).where(
                OAuthState.provider == provider,
                OAuthState.state_token == state_token,
                OAuthState.consumed_at.is_(None),
                OAuthState.expires_at > utcnow(),
            )
        )
        if not state:
            raise AppError("AUTH_ERROR", "Invalid or consumed OAuth state", 401)
        state.consumed_at = utcnow()
        draft = self.db.get(
            CalendarConnection if provider == "google" else Source,
            state.calendar_connection_id if provider == "google" else state.source_id,
        )
        if draft:
            draft.status = "error"
        self.db.commit()

    def refresh_source(self, source: Source, credential: SourceCredential) -> None:
        if not credential.encrypted_refresh_token:
            raise AppError("MAIL_REAUTH_REQUIRED", "Reconnect the mailbox", 401)
        provider = source.type
        client_id, client_secret = self._client_credentials(provider)
        refresh = SecretBox().decrypt(credential.encrypted_refresh_token)
        try:
            response = httpx.post(
                PROVIDERS[provider][1],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=15,
            )
            response.raise_for_status()
            tokens = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("MAIL_REAUTH_REQUIRED", "Token refresh failed", 401) from exc
        if not tokens.get("access_token"):
            raise AppError("MAIL_REAUTH_REQUIRED", "Token refresh failed", 401)
        credential.encrypted_access_token = SecretBox().encrypt(tokens["access_token"])
        if tokens.get("refresh_token"):
            credential.encrypted_refresh_token = SecretBox().encrypt(tokens["refresh_token"])
        credential.token_expires_at = utcnow() + timedelta(
            seconds=int(tokens.get("expires_in", 3600))
        )
        self.db.commit()
