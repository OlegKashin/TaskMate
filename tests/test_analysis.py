from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.models.entities import AIProcessingJob, Message, Source
from app.services.analysis import queue_source_analysis
from app.services.domain import UserService
from app.workers.tasks import process_message, sync_mail_source


def test_analysis_queues_real_messages_only(db, monkeypatch):
    user = UserService(db).get_or_create(880)
    source = Source(user_id=user.id, type="telegram_chat", name="Chat", status="active", external_source_id="880")
    db.add(source)
    db.flush()
    message = Message(
        user_id=user.id, source_id=source.id, external_message_id="1", message_type="text",
        text="Create a task", received_at=datetime.now(UTC), processing_status="received",
    )
    db.add(message)
    db.commit()
    delay = Mock()
    monkeypatch.setattr(process_message, "delay", delay)
    result = queue_source_analysis(db, user)
    assert result == {"status": "queued", "queued": 1, "external_sync": "not_applicable", "mail_jobs": 0}
    assert message.processing_status == "queued"
    assert db.scalar(select(AIProcessingJob).where(AIProcessingJob.message_id == message.id))
    delay.assert_called_once()
    assert queue_source_analysis(db, user)["status"] == "completed"
    delay.assert_called_once()


def test_analysis_queues_email_sync_without_blocking(db, monkeypatch):
    user = UserService(db).get_or_create(881)
    source = Source(user_id=user.id, type="gmail", name="Mail", status="active", external_source_id="a@b")
    db.add(source)
    db.commit()
    monkeypatch.setattr(process_message, "delay", Mock())
    delayed = Mock()
    monkeypatch.setattr(sync_mail_source, "delay", delayed)
    result = queue_source_analysis(db, user, source.id)
    assert result["queued"] == 0 and result["external_sync"] == "queued"
    delayed.assert_called_once_with(str(source.id))
    source.status = "disconnected"
    db.commit()
    with pytest.raises(AppError):
        queue_source_analysis(db, user, source.id)
