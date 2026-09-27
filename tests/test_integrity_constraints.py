"""Ownership and database constraints that service-level checks cannot replace."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError
from app.models.entities import CalendarConnection, CalendarEvent, Message, Project, Source
from app.services.domain import (
    CalendarService,
    ReminderService,
    TaskService,
    UserService,
    WaitingService,
)


def test_cross_user_references_are_rejected(db):
    owner = UserService(db).get_or_create(9301)
    other = UserService(db).get_or_create(9302)
    other_task, _ = TaskService(db).create(other, {"title": "Private"})
    source = Source(user_id=other.id, type="telegram_chat", name="Other", status="active")
    db.add(source)
    db.flush()
    other_message = Message(
        user_id=other.id, source_id=source.id, external_message_id="private-1",
        message_type="text", text="Private", received_at=datetime.now(UTC),
    )
    db.add(other_message)
    connection = CalendarConnection(user_id=owner.id, provider="google", status="active")
    db.add(connection)
    db.commit()

    with pytest.raises(AppError) as exc:
        ReminderService(db).create(owner, {
            "title": "Cross-account", "due_at": datetime.now(UTC) + timedelta(hours=1),
            "task_id": other_task.id,
        })
    assert exc.value.status_code == 404
    with pytest.raises(AppError) as exc:
        CalendarService(db).create(owner, {
            "connection_id": connection.id, "task_id": other_task.id,
            "title": "Cross-account", "start_at": datetime.now(UTC) + timedelta(hours=1),
            "end_at": datetime.now(UTC) + timedelta(hours=2),
        })
    assert exc.value.status_code == 404
    with pytest.raises(AppError):
        TaskService(db).create(owner, {
            "title": "Cross-account", "source_message_id": other_message.id,
        })
    with pytest.raises(AppError):
        WaitingService(db).create(owner, {
            "title": "Cross-account", "message_id": other_message.id,
        })


def test_partial_unique_indexes_enforce_active_records(db):
    user = UserService(db).get_or_create(9310)
    first = Project(user_id=user.id, name="Work")
    db.add(first)
    db.commit()
    db.add(Project(user_id=user.id, name="Work"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    first.is_archived = True
    db.commit()
    db.add(Project(user_id=user.id, name="Work"))
    db.commit()

    task, _ = TaskService(db).create(user, {"title": "Meeting"})
    connection = CalendarConnection(user_id=user.id, provider="google", status="active")
    db.add(connection)
    db.commit()
    start = datetime.now(UTC) + timedelta(days=1)
    first_event = CalendarEvent(
        user_id=user.id, connection_id=connection.id, task_id=task.id,
        title="First", start_at=start, end_at=start + timedelta(hours=1), status="pending",
    )
    db.add(first_event)
    db.commit()
    db.add(CalendarEvent(
        user_id=user.id, connection_id=connection.id, task_id=task.id,
        title="Duplicate", start_at=start, end_at=start + timedelta(hours=1), status="pending",
    ))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    first_event.status = "cancelled"
    db.commit()
    db.add(CalendarEvent(
        user_id=user.id, connection_id=connection.id, task_id=task.id,
        title="Replacement", start_at=start, end_at=start + timedelta(hours=1), status="pending",
    ))
    db.commit()
