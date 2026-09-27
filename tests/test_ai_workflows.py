import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.ai.service import AIActionService, AIService
from app.bot.presentation import outcome_message, section_message
from app.core.errors import AppError
from app.core.security import issue_user_token, verify_user_token
from app.models.entities import AIProcessingJob, Message, Source
from app.schemas.domain import AIAction, AIResult
from app.services.domain import ProjectService, TaskService, UserService


def result(intent, entities, confidence=0.9, confirmation=True, classification=None):
    return AIResult(
        intent=intent, confidence=confidence, entities=entities,
        action=AIAction(type=intent, requires_confirmation=confirmation),
        reason="Проверка", inbox_classification=classification,
    )


def test_user_token_cannot_switch_identity(client, headers):
    assert verify_user_token(issue_user_token(1001)) == 1001
    forged = {"Authorization": headers["Authorization"].replace("tm1.1001.", "tm1.2002.")}
    assert client.get("/api/v1/me", headers=forged).status_code == 401
    assert client.get("/api/v1/me", headers={**headers, "X-Telegram-User-Id": "2002"}).json()["data"]["telegram_user_id"] == 1001
    with pytest.raises(AppError):
        verify_user_token(issue_user_token(1001, expires_in=-1))


def test_ai_schema_and_backend_confirmation(db):
    user = UserService(db).get_or_create(101)
    with pytest.raises(ValidationError):
        result("create_task", {})
    with pytest.raises(ValidationError):
        result("change_task_status", {"target": {"task_id": str(uuid.uuid4())}, "new_status": "bad"})
    with pytest.raises(ValidationError):
        result("ignore", {}, classification={"item_type": "invalid"})
    proposal = AIActionService(db).apply(user, result("create_task", {"title": "Отчёт"}, confirmation=False))
    assert proposal["state"] == "proposal"
    text, markup = outcome_message(db, user, proposal)
    assert "Отчёт" in text and len(markup["inline_keyboard"][0]) == 2
    medium = AIActionService(db).apply(user, result("create_task", {"title": "Доклад"}, confidence=0.7))
    _, medium_markup = outcome_message(db, user, medium)
    assert len(medium_markup["inline_keyboard"][0]) == 3
    assert AIActionService(db).apply(user, result("create_task", {"title": "x"}, confidence=0.2))["state"] == "clarification"


def test_ai_task_mutations_and_presentation(db):
    user = UserService(db).get_or_create(102)
    service = AIActionService(db)
    project = ProjectService(db).create(user, {"name": "Работа"})
    created = service.execute(user, result("create_task", {"title": "Отчёт", "project_hint": "Работа"}))
    task = TaskService(db).get(user, uuid.UUID(created["object_id"]))
    assert task.project_id == project.id
    text, markup = outcome_message(db, user, created)
    assert "выполнено" in text and markup["inline_keyboard"][0][0]["text"] == "Отменить"
    target = {"task_id": str(task.id)}
    edited = service.execute(user, result("edit_task", {"target": target, "changed_fields": {"project_hint": None, "title": "Новый отчёт"}}))
    assert edited["state"] == "executed" and task.project_id is None
    status = service.execute(user, result("change_task_status", {"target": target, "new_status": "completed"}))
    assert status["state"] == "executed" and task.status == "completed"
    deleted = service.execute(user, result("delete_task", {"target": target}))
    assert deleted["state"] == "executed" and task.deleted_at is not None
    with pytest.raises(AppError):
        TaskService(db).undo(user, deleted["undo"], expected_task_id=uuid.uuid4())
    assert task.deleted_at is not None


def test_ai_classification_creates_inbox_and_notification(db):
    user = UserService(db).get_or_create(103)
    source = Source(user_id=user.id, type="telegram_chat", name="T", status="active", external_source_id="103")
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="one",
                      message_type="text", text="Срочно!", received_at=datetime.now(UTC))
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(job)
    db.commit()

    class Provider:
        async def interpret(self, text):
            return result("ignore", {"reason": "x"}, confirmation=False,
                          classification={"item_type": "urgent", "priority": "high"})

    assert asyncio.run(AIService(db, Provider()).process(job, message, user))["state"] == "informational"
    from sqlalchemy import select

    from app.models.entities import InboxItem, Notification

    item = db.scalar(select(InboxItem).where(InboxItem.message_id == message.id))
    note = db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert item.item_type == "urgent" and item.priority == "high"
    assert note.type == "urgent_item"
    assert note.payload["title"].startswith("🔴 Срочно\n")


def test_bot_section_views(db):
    user = UserService(db).get_or_create(104)
    user.timezone = "Europe/Moscow"
    now = datetime.now(UTC)
    TaskService(db).create(user, {"title": "Сегодня", "due_at": now + timedelta(hours=1)})
    assert "Сегодня" in section_message(db, user, "today")
    assert "Создано задач" in section_message(db, user, "stats")
    assert "Настройки" in section_message(db, user, "settings")
    assert "Задачи" in section_message(db, user, "tasks")
    assert "Событий" in section_message(db, user, "schedule")
    assert "не найден" in section_message(db, user, "unknown")


def test_ai_search_returns_openable_results(db):
    user = UserService(db).get_or_create(105)
    TaskService(db).create(user, {"title": "Подготовить бюджет"})
    TaskService(db).create(user, {"title": "Позвонить Ивану"})
    outcome = AIActionService(db).apply(user, result("search_task", {"query": "бюджет"}))
    assert outcome["state"] == "search_results"
    assert [task["title"] for task in outcome["tasks"]] == ["Подготовить бюджет"]
    text, markup = outcome_message(db, user, outcome)
    assert "Нашёл задач: 1" in text
    assert markup["inline_keyboard"][0][0]["callback_data"].startswith("a:")


def test_ambiguous_task_choice_precedes_confirmation(client, db, monkeypatch):
    from app.integrations.telegram.client import TelegramClient

    user = UserService(db).get_or_create(106)
    first, _ = TaskService(db).create(user, {"title": "Отчёт по продажам"})
    second, _ = TaskService(db).create(user, {"title": "Отчёт по расходам"})
    requested = result("edit_task", {
        "target": {"search_hint": {"title_contains": "Отчёт"}},
        "changed_fields": {"title": "Новый отчёт"},
    })
    outcome = AIActionService(db).apply(user, requested)
    assert outcome["state"] == "clarification" and len(outcome["candidates"]) == 2
    _, markup = outcome_message(db, user, outcome)
    edited = []
    monkeypatch.setattr(TelegramClient, "edit_message", lambda self, *args: edited.append(args))
    monkeypatch.setattr(TelegramClient, "answer_callback", lambda *args: None)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    def click(update_id, token):
        return client.post("/webhooks/telegram", headers=headers, json={
            "update_id": update_id, "callback_query": {
                "id": str(update_id), "from": {"id": 106}, "data": token,
                "message": {"message_id": 10, "chat": {"id": 106, "type": "private"}},
            },
        })

    select_token = markup["inline_keyboard"][0][0]["callback_data"]
    assert click(10601, select_token).status_code == 200
    assert first.title == "Отчёт по продажам" and second.title == "Отчёт по расходам"
    confirm_token = edited[-1][3]["inline_keyboard"][0][0]["callback_data"]
    assert click(10602, confirm_token).status_code == 200
    assert first.title == "Новый отчёт" and second.title == "Отчёт по расходам"


def test_low_confidence_incomplete_actions_request_clarification(db):
    user = UserService(db).get_or_create(107)
    for intent in ("project_action", "source_action", "change_task_status"):
        uncertain = result(intent, {}, confidence=0.3)
        assert AIActionService(db).apply(user, uncertain)["state"] == "clarification"
