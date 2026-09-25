"""Google Calendar transport, including encrypted refresh-token rotation."""

from datetime import UTC, timedelta
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import SecretBox
from app.models.entities import CalendarConnection, CalendarEvent, utcnow

BASE_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleCalendarAPI:
    def __init__(self, db: Session):
        self.db = db

    def access_token(self, connection: CalendarConnection) -> str:
        box = SecretBox()
        expiry = connection.token_expires_at
        if expiry and not expiry.tzinfo:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry and expiry <= utcnow() + timedelta(minutes=1):
            settings = get_settings()
            if not connection.encrypted_refresh_token or not settings.google_client_id or not settings.google_client_secret:
                raise AppError("CALENDAR_RECONNECT_REQUIRED", "Reconnect Google Calendar", 409)
            try:
                response = httpx.post(TOKEN_URL, data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": box.decrypt(connection.encrypted_refresh_token),
                    "grant_type": "refresh_token",
                }, timeout=15)
                response.raise_for_status()
                tokens = response.json()
                access_token = tokens["access_token"]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise AppError("CALENDAR_REFRESH_FAILED", "Calendar token refresh failed", 502) from exc
            connection.encrypted_access_token = box.encrypt(access_token)
            if tokens.get("refresh_token"):
                connection.encrypted_refresh_token = box.encrypt(tokens["refresh_token"])
            if tokens.get("expires_in"):
                connection.token_expires_at = utcnow() + timedelta(seconds=int(tokens["expires_in"]))
            self.db.commit()
            return access_token
        if not connection.encrypted_access_token:
            raise AppError("CALENDAR_RECONNECT_REQUIRED", "Reconnect Google Calendar", 409)
        return box.decrypt(connection.encrypted_access_token)

    def save(self, connection: CalendarConnection, event: CalendarEvent) -> str:
        token = self.access_token(connection)
        event_id = event.external_event_id or event.id.hex
        start = event.start_at if event.start_at.tzinfo else event.start_at.replace(tzinfo=UTC)
        end = event.end_at if event.end_at.tzinfo else event.end_at.replace(tzinfo=UTC)
        body = {
            "id": event_id,
            "summary": event.title,
            "description": event.description or "",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if event.external_event_id:
                response = httpx.put(f"{BASE_URL}/{quote(event_id, safe='')}", headers=headers, json=body, timeout=15)
            else:
                response = httpx.post(BASE_URL, headers=headers, json=body, timeout=15)
                if response.status_code == 409:
                    response = httpx.get(f"{BASE_URL}/{event_id}", headers=headers, timeout=15)
            response.raise_for_status()
            return str(response.json()["id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AppError("CALENDAR_SYNC_FAILED", "Calendar event sync failed", 502) from exc

    def delete(self, connection: CalendarConnection, event: CalendarEvent) -> None:
        if not event.external_event_id:
            return
        token = self.access_token(connection)
        try:
            response = httpx.delete(
                f"{BASE_URL}/{quote(event.external_event_id, safe='')}",
                headers={"Authorization": f"Bearer {token}"}, timeout=15,
            )
            if response.status_code != 404:
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError("CALENDAR_CANCEL_FAILED", "Calendar event cancellation failed", 502) from exc
