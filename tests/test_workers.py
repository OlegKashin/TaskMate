import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.models.entities import AIProcessingJob, Message, Source, UserSettings
from app.services.domain import ReminderService, UserService
from app.workers import tasks


def job_for(db, kind="text", text="Как дела?"):
    user = UserService(db).get_or_create(770)
    source = db.scalar(
        select(Source).where(Source.user_id == user.id, Source.external_source_id == "770")
    )
    if not source:
        source = Source(user_id=user.id, type="telegram_chat", name="Chat", status="active", external_source_id="770")
        db.add(source)
        db.flush()
    message = Message(
        user_id=user.id, source_id=source.id, external_message_id=str(uuid.uuid4()),
        message_type=kind, text=text, received_at=datetime.now(UTC),
        raw_payload={"message": {"chat": {"id": 770}}}, processing_status="queued",
    )
    db.add(message)
    db.flush()
    job = AIProcessingJob(
        user_id=user.id, message_id=message.id,
        job_type="attachment_only" if kind == "photo" else "interpret", status="queued",
        status_message_id=123,
    )
    db.add(job)
    db.commit()
    return user, message, job


def test_worker_processes_text_and_attachment(db, monkeypatch):
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))
    client = Mock()
    monkeypatch.setattr(tasks, "TelegramClient", lambda: client)
    _user, message, job = job_for(db)
    outcome = tasks.process_message.run(str(job.id))
    assert outcome["state"] == "informational" and job.status == "completed"
    client.edit_message.assert_called_once()
    monkeypatch.setattr(tasks, "save_telegram_attachment", lambda db, message: Mock())
    _user, photo, photo_job = job_for(db, "photo", None)
    assert tasks.process_message.run(str(photo_job.id))["reason"] == "Вложение сохранено."
    assert photo.processing_status == "processed"


def test_worker_reports_voice_failure(db, monkeypatch):
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))
    client = Mock()
    monkeypatch.setattr(tasks, "TelegramClient", lambda: client)
    _user, message, job = job_for(db, "voice", None)
    job.job_type = "stt"
    db.commit()
    with pytest.raises(AppError):
        tasks.process_message.run(str(job.id))
    assert job.status == "failed" and job.error_code == "STT_NOT_CONFIGURED"
    assert "голосовых" in client.edit_message.call_args.args[2]


def test_scheduler_delivers_reminder(db, monkeypatch):
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))
    client = Mock()
    client.send_message.return_value = {"message_id": 5}
    monkeypatch.setattr("app.integrations.telegram.client.TelegramClient", lambda: client)
    user = UserService(db).get_or_create(771)
    settings = db.get(UserSettings, user.id)
    settings.morning_briefing_enabled = settings.evening_stats_enabled = False
    db.commit()
    ReminderService(db).create(user, {
        "title": "Ping", "due_at": datetime.now(UTC) - timedelta(minutes=1),
    })
    result = tasks.schedule_tick.run()
    assert result["reminders_fired"] == 1 and result["notifications_sent"] == 1
    client.send_message.assert_called_with(771, "Ping")
