from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from sqlalchemy import select

from app.models.entities import Notification, UserSettings
from app.services.domain import NotificationService, ReminderService, UserService


def test_briefings_are_local_and_deduplicated(db):
    user = UserService(db).get_or_create(701)
    user.timezone = "Europe/Moscow"
    settings = db.get(UserSettings, user.id)
    settings.morning_briefing_time = datetime(2026, 1, 1, 8, 0).time()
    settings.evening_stats_enabled = False
    db.commit()
    service = NotificationService(db)
    now = datetime(2026, 1, 1, 5, 5, tzinfo=UTC)
    assert service.enqueue_briefings(now) == 1
    assert service.enqueue_briefings(now) == 0
    assert service.enqueue_briefings(now + timedelta(minutes=16)) == 0
    client = Mock()
    client.send_message.return_value = {"message_id": 123}
    assert service.deliver_pending(client) == 1
    assert service.deliver_pending(client) == 0
    client.send_message.assert_called_once()
    note = db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert note.status == "sent" and note.sent_at is not None


def test_failed_notification_remains_pending(db):
    user = UserService(db).get_or_create(702)
    reminder = ReminderService(db).create(
        user, {"title": "Ping", "due_at": datetime.now(UTC) - timedelta(minutes=1)}
    )
    assert ReminderService(db).fire_due() == 1
    client = Mock()
    client.send_message.side_effect = RuntimeError("offline")
    assert NotificationService(db).deliver_pending(client) == 0
    note = db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert note.status == "pending"
    client.send_message.side_effect = None
    client.send_message.return_value = {"message_id": 5}
    assert NotificationService(db).deliver_pending(client) == 1
    client.send_message.assert_called_with(702, reminder.title)
