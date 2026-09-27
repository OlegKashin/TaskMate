from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.integrations.telegram.client import TelegramClient
from app.models.entities import CalendarConnection, CalendarEvent, Message, Task
from app.services.domain import SourceService, UserService


def test_today_and_stats_follow_user_local_day(client, db, headers):
    user = UserService(db).get_or_create(1001)
    user.timezone = "Pacific/Kiritimati"
    zone = ZoneInfo(user.timezone)
    start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    today = (start + timedelta(hours=2)).astimezone(UTC)
    tomorrow = (start + timedelta(days=1, hours=2)).astimezone(UTC)
    connection = CalendarConnection(user_id=user.id, status="active")
    db.add(connection)
    db.flush()
    db.add_all([
        Task(user_id=user.id, title="Today", due_at=today, created_at=today),
        Task(user_id=user.id, title="Tomorrow", due_at=tomorrow, created_at=tomorrow),
        CalendarEvent(user_id=user.id, connection_id=connection.id,
                      title="Today event", start_at=today,
                      end_at=today + timedelta(hours=1), status="confirmed"),
    ])
    db.commit()

    response = client.get("/api/v1/today", headers=headers)
    assert response.status_code == 200
    assert [task["title"] for task in response.json()["data"]["tasks"]] == ["Today"]
    assert [event["title"] for event in response.json()["data"]["events"]] == ["Today event"]
    assert client.get("/api/v1/stats", headers=headers).json()["data"]["tasks"] == 1


def test_telegram_ignores_preconnection_history_but_stores_disabled_messages(
    client, db, monkeypatch
):
    user = UserService(db).get_or_create(8661)
    source = SourceService(db).create(user, {
        "type": "telegram_chat", "name": "Telegram", "external_source_id": "8661",
    })
    source.connected_at = datetime.now(UTC) - timedelta(minutes=10)
    source.analysis_text = False
    source.analysis_voice = False
    source.save_attachments = False
    db.commit()
    send = Mock(return_value=None)
    monkeypatch.setattr(TelegramClient, "send_message", send)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    def update(update_id, date):
        return {"update_id": update_id, "message": {
            "message_id": update_id, "date": int(date.timestamp()),
            "from": {"id": 8661}, "chat": {"id": 8661, "type": "private"},
            "text": "Message",
        }}

    old = client.post(
        "/webhooks/telegram", headers=headers,
        json=update(991, datetime.now(UTC) - timedelta(minutes=20)),
    )
    assert old.json()["reason"] == "before_source_connected"
    current = client.post(
        "/webhooks/telegram", headers=headers, json=update(992, datetime.now(UTC)),
    )
    assert current.json()["reason"] == "source_analysis_disabled"
    messages = db.scalars(select(Message).where(Message.source_id == source.id)).all()
    assert len(messages) == 1
    assert messages[0].external_message_id == "992"
    assert messages[0].processing_status == "ignored"
    assert not send.called
