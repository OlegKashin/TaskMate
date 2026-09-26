from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.service import AIActionService
from app.bot.actions import handle_action
from app.bot.views import object_view, section_view
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import SecretBox, issue_user_token
from app.integrations.calendar import GoogleCalendarAPI
from app.models.entities import CalendarConnection, CalendarEvent, Notification
from app.schemas.domain import AIAction, AIResult
from app.services.domain import CalendarService, NotificationService, UIActionService, UserService
from app.services.oauth import OAuthService


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


def connection_and_event(db):
    user = UserService(db).get_or_create(800)
    box = SecretBox()
    connection = CalendarConnection(
        user_id=user.id, provider="google", status="active", external_account_id="calendar-id",
        encrypted_access_token=box.encrypt("old-access"),
        encrypted_refresh_token=box.encrypt("refresh"),
    )
    db.add(connection)
    db.commit()
    start = datetime.now(UTC) + timedelta(days=1)
    event = CalendarService(db).create(user, {
        "connection_id": connection.id, "title": "Meeting", "start_at": start,
        "end_at": start + timedelta(hours=1),
    })
    return user, connection, event


def test_calendar_sync_update_cancel(db, monkeypatch):
    user, connection, event = connection_and_event(db)
    assert event.status == "pending"
    post = Mock(return_value=response({"id": event.id.hex}))
    monkeypatch.setattr(httpx, "post", post)
    assert CalendarService(db).sync_pending() == 1
    assert event.status == "confirmed" and event.external_event_id == event.id.hex
    assert post.call_args.kwargs["json"]["start"]["dateTime"].endswith("+00:00")
    monkeypatch.setattr(httpx, "put", Mock(return_value=response({"id": event.id.hex})))
    CalendarService(db).patch(user, event.id, {"title": "Changed"})
    assert event.status == "pending"
    assert CalendarService(db).sync_pending() == 1
    monkeypatch.setattr(httpx, "delete", Mock(return_value=response({}, 204)))
    assert CalendarService(db).cancel(user, event.id).status == "cancelled"
    assert CalendarService(db).sync_pending() == 0


def test_calendar_retry_refresh_and_reconnect(db, monkeypatch):
    _user, connection, event = connection_and_event(db)
    connection.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "post", Mock(return_value=response({
        "access_token": "fresh", "refresh_token": "rotated", "expires_in": 3600,
    })))
    assert GoogleCalendarAPI(db).access_token(connection) == "fresh"
    assert SecretBox().decrypt(connection.encrypted_refresh_token) == "rotated"
    monkeypatch.setattr(httpx, "post", Mock(return_value=response({}, 409)))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response({"id": event.id.hex})))
    assert GoogleCalendarAPI(db).save(connection, event) == event.id.hex
    connection.encrypted_access_token = None
    connection.encrypted_refresh_token = None
    connection.token_expires_at = None
    db.commit()
    with pytest.raises(AppError) as exc:
        GoogleCalendarAPI(db).access_token(connection)
    assert exc.value.code == "CALENDAR_RECONNECT_REQUIRED"
    get_settings.cache_clear()


def test_calendar_transport_failure_leaves_pending(db, monkeypatch):
    user, connection, event = connection_and_event(db)
    monkeypatch.setattr(httpx, "post", Mock(side_effect=httpx.ConnectError("offline")))
    assert CalendarService(db).sync_pending() == 0
    assert event.status == "pending"
    event.external_event_id = "remote-id"
    db.commit()
    monkeypatch.setattr(httpx, "delete", Mock(side_effect=httpx.ConnectError("offline")))
    with pytest.raises(AppError):
        CalendarService(db).cancel(user, event.id)
    assert event.status == "pending"
    connection.status = "disconnected"
    db.commit()
    assert CalendarService(db).sync_pending() == 0


def test_calendar_retry_schedule_and_bot_actions(db, monkeypatch):
    user, _connection, event = connection_and_event(db)
    monkeypatch.setattr(GoogleCalendarAPI, "save", Mock(side_effect=AppError(
        "CALENDAR_SYNC_FAILED", "offline", 502,
    )))
    service = CalendarService(db)
    for attempt, delay in enumerate((30, 120, 600), 1):
        before = datetime.now(UTC)
        assert service.sync_pending() == 0
        assert event.sync_attempts == attempt
        assert delay - 2 <= (event.next_sync_at.replace(tzinfo=UTC) - before).total_seconds() <= delay + 2
        event.next_sync_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert service.sync_pending() == 0
    assert event.sync_attempts == 4 and event.next_sync_at is None
    assert service.sync_pending() == 0
    assert db.query(Notification).filter_by(user_id=user.id, type="calendar_error").count() == 1
    client = Mock()
    client.send_message.return_value = {"message_id": 1}
    assert NotificationService(db).deliver_pending(client) == 1
    assert client.send_message.call_args.args[2]["inline_keyboard"][0][0]["text"] == "Повторить"

    text, markup = section_view(db, user, "schedule")
    assert markup and "Meeting" in text + str(markup)
    text, markup = object_view(db, user, "schedule", event.id)
    assert "Meeting" in text
    retry_token = markup["inline_keyboard"][0][0]["callback_data"]
    action = UIActionService(db).consume(user, retry_token, "calendar_retry")
    text, _ = handle_action(db, user, action)
    assert "запланирована" in text
    assert event.sync_attempts == 0
    monkeypatch.setattr(GoogleCalendarAPI, "save", Mock(return_value="remote-id"))
    assert service.sync_pending() == 1
    assert event.status == "confirmed"

    text, markup = object_view(db, user, "schedule", event.id)
    cancel_token = markup["inline_keyboard"][0][0]["callback_data"]
    action = UIActionService(db).consume(user, cancel_token, "calendar_cancel_prompt")
    text, markup = handle_action(db, user, action)
    assert "Отменить событие" in text and "Google Calendar" in text and "Undo" in text
    retry_failed = Mock(side_effect=AppError("CALENDAR_CANCEL_FAILED", "offline", 502))
    monkeypatch.setattr(GoogleCalendarAPI, "delete", retry_failed)
    confirm_token = markup["inline_keyboard"][0][0]["callback_data"]
    action = UIActionService(db).consume(user, confirm_token, "calendar_cancel")
    text, markup = handle_action(db, user, action)
    assert "Повторить" in str(markup) and event.status == "confirmed"
    monkeypatch.setattr(GoogleCalendarAPI, "delete", Mock())
    confirm_token = markup["inline_keyboard"][0][0]["callback_data"]
    action = UIActionService(db).consume(user, confirm_token, "calendar_cancel")
    text, _ = handle_action(db, user, action)
    assert "отменено" in text and event.status == "cancelled"


def test_calendar_reconnect_error_and_retry_api(db, client, monkeypatch):
    user, connection, event = connection_and_event(db)
    monkeypatch.setattr(GoogleCalendarAPI, "save", Mock(side_effect=AppError(
        "CALENDAR_RECONNECT_REQUIRED", "token invalid", 401,
    )))
    assert CalendarService(db).sync_pending() == 0
    assert connection.status == "error" and event.sync_attempts == 4
    sender = Mock()
    sender.send_message.return_value = {"message_id": 1}
    assert NotificationService(db).deliver_pending(sender) == 1
    assert sender.send_message.call_args.args[2]["inline_keyboard"][0][0]["text"] == "Переподключить"
    headers = {"Authorization": f"Bearer {issue_user_token(user.telegram_user_id)}"}
    response = client.post(f"/api/v1/calendar/events/{event.id}/retry", headers=headers)
    assert response.status_code == 409
    connection.status = "active"
    db.commit()
    response = client.post(f"/api/v1/calendar/events/{event.id}/retry", headers=headers)
    assert response.status_code == 200
    assert event.sync_attempts == 0


def test_pending_local_calendar_event_can_be_cancelled_offline(db, monkeypatch):
    user, connection, event = connection_and_event(db)
    connection.status = "error"
    db.commit()
    delete = Mock()
    monkeypatch.setattr(GoogleCalendarAPI, "delete", delete)
    assert CalendarService(db).cancel(user, event.id).status == "cancelled"
    assert CalendarService(db).cancel(user, event.id).status == "cancelled"
    delete.assert_not_called()


def test_ai_calendar_and_unsafe_actions(db):
    user, connection, _event = connection_and_event(db)
    start = datetime.now(UTC) + timedelta(days=3)
    result = AIResult(
        intent="create_event", confidence=0.9,
        entities={"title": "AI meeting", "start_at": start.isoformat(),
                  "end_at": (start + timedelta(hours=1)).isoformat()},
        action=AIAction(type="create_event", requires_confirmation=False),
    )
    proposal = AIActionService(db).apply(user, result)
    assert proposal["state"] == "proposal"
    outcome = AIActionService(db).execute(user, result)
    assert outcome["state"] == "executed"
    created = db.get(CalendarEvent, __import__("uuid").UUID(outcome["object_id"]))
    assert created.connection_id == connection.id and created.status == "pending"
    email = AIResult(
        intent="reply_email", confidence=0.9,
        entities={"message_id": "x", "draft_text": "Hello"}, action=AIAction(type="reply_email"),
    )
    assert AIActionService(db).apply(user, email)["state"] == "proposal"
    with pytest.raises(AppError) as exc:
        AIActionService(db).execute(user, email)
    assert exc.value.status_code == 422
    for intent, entities in (
        ("project_action", {"action": "destroy"}),
        ("source_action", {"action": "destroy", "source_id": "x"}),
    ):
        with pytest.raises(ValidationError):
            AIResult(intent=intent, confidence=0.9, entities=entities, action=AIAction(type=intent))


def test_oauth_real_http_exchange_and_provider_identity(db, monkeypatch):
    user = UserService(db).get_or_create(801)
    monkeypatch.setenv("GMAIL_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    get_settings.cache_clear()
    service = OAuthService(db)
    state = service.create_state(user, "gmail")
    token_call = Mock(return_value=response({
        "access_token": "provider-token", "refresh_token": "refresh", "expires_in": 3600,
    }))
    profile_call = Mock(return_value=response({"emailAddress": "verified@example.com"}))
    monkeypatch.setattr(httpx, "post", token_call)
    monkeypatch.setattr(httpx, "get", profile_call)
    source = service.complete("gmail", state.state_token, "code")
    assert source.external_source_id == "verified@example.com"
    assert token_call.call_args.kwargs["data"]["code"] == "code"
    assert profile_call.call_args.kwargs["headers"] == {"Authorization": "Bearer provider-token"}
    assert db.scalar(select(Notification).where(Notification.user_id == user.id)).type == "oauth_connected"
    get_settings.cache_clear()


def test_oauth_failure_consumes_state_without_credentials(db, monkeypatch):
    user = UserService(db).get_or_create(802)
    monkeypatch.setenv("GMAIL_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    get_settings.cache_clear()
    service = OAuthService(db)
    state = service.create_state(user, "gmail")
    monkeypatch.setattr(httpx, "post", Mock(side_effect=httpx.ConnectError("offline")))
    with pytest.raises(AppError) as exc:
        service.complete("gmail", state.state_token, "code")
    assert exc.value.code == "OAUTH_EXCHANGE_FAILED"
    with pytest.raises(AppError):
        service.complete("gmail", state.state_token, "code")
    get_settings.cache_clear()


def test_production_rejects_example_secrets():
    with pytest.raises(ValidationError):
        Settings(app_env="production", _env_file=None)
