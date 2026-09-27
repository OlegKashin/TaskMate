import asyncio
import base64
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import select

from app.ai.service import AIActionService, AIService
from app.bot.actions import handle_action
from app.bot.presentation import outcome_message
from app.bot.views import inbox_view, object_view, section_view
from app.core.errors import AppError
from app.core.security import SecretBox
from app.models.entities import (
    AIProcessingJob,
    InboxItem,
    Message,
    Project,
    Source,
    SourceCredential,
    SourceFolder,
    Task,
    UserSettings,
)
from app.schemas.domain import AIAction, AIResult
from app.services.domain import (
    ProjectService,
    SourceService,
    TaskService,
    UIActionService,
    UserService,
)
from app.services.email import EmailService
from app.workers import tasks


def test_email_intent_creates_reopenable_inbox_proposal(db):
    user = UserService(db).get_or_create(5001)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com", connected_at=datetime.now(UTC))
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="mail-1",
                      message_type="email", subject="Отчёт", text="Подготовить отчёт",
                      received_at=datetime.now(UTC))
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(job)
    db.commit()

    class Provider:
        async def interpret(self, text):
            return AIResult(intent="create_task", confidence=0.91,
                            entities={"title": "Отчёт"}, action=AIAction(type="create_task"),
                            reason="Письмо содержит поручение")

    outcome = asyncio.run(AIService(db, Provider()).process(job, message, user))
    item = db.scalar(select(InboxItem).where(InboxItem.message_id == message.id))
    assert item.item_type == "task_candidate" and item.status == "proposed"
    UIActionService(db).consume(user, outcome["token"], "confirm_ai")
    db.commit()
    text, markup = inbox_view(db, user, item.id)
    assert "Отчёт" in text
    fresh = markup["inline_keyboard"][0][0]["callback_data"]
    assert fresh != outcome["token"]
    action = UIActionService(db).consume(user, fresh, "confirm_ai")
    executed = AIActionService(db).execute(user, AIResult.model_validate(action.payload["result"]), message)
    assert executed["state"] == "executed"


def test_low_confidence_can_request_missing_fields_without_proposal(db):
    user = UserService(db).get_or_create(5009)
    uncertain = AIResult(intent="create_task", confidence=0.35, entities={},
                         action=AIAction(type="create_task"), reason="Как назвать задачу?")
    outcome = AIActionService(db).apply(user, uncertain)
    assert outcome == {"state": "clarification", "reason": "Как назвать задачу?"}


def test_event_without_calendar_offers_connection(db):
    user = UserService(db).get_or_create(5011)
    event = AIResult(intent="create_event", confidence=0.9,
                     entities={"title": "Встреча", "start_at": "2026-10-01T10:00:00+03:00",
                               "end_at": "2026-10-01T11:00:00+03:00"},
                     action=AIAction(type="create_event"))
    outcome = AIActionService(db).apply(user, event)
    assert outcome["state"] == "calendar_not_connected"
    text, markup = outcome_message(db, user, outcome)
    assert "Google Calendar" in text
    assert {button["text"] for button in markup["inline_keyboard"][0]} == {"Подключить", "Отмена"}


def test_uncertain_email_is_visible_as_question(db):
    user = UserService(db).get_or_create(5010)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com")
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="uncertain",
                      message_type="email", subject="Сделать?", text="Пожалуйста, займись этим",
                      received_at=datetime.now(UTC))
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(job)
    db.commit()

    class Provider:
        async def interpret(self, text):
            return AIResult(intent="create_task", confidence=0.3, entities={},
                            action=AIAction(type="create_task"), reason="Что именно сделать?")

    assert asyncio.run(AIService(db, Provider()).process(job, message, user))["state"] == "clarification"
    item = db.scalar(select(InboxItem).where(InboxItem.message_id == message.id))
    assert item.item_type == "question" and item.status == "new"
    text, markup = inbox_view(db, user, item.id)
    assert "Что именно" in text
    assert any(button["text"] == "Уточнить" for row in markup["inline_keyboard"] for button in row)


def test_folder_discovery_and_selection(db, monkeypatch):
    user = UserService(db).get_or_create(5002)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com", connected_at=datetime.now(UTC))
    db.add(source)
    db.flush()
    db.add(SourceCredential(source_id=source.id, provider="gmail",
                            encrypted_access_token=SecretBox().encrypt("token"),
                            encrypted_username=SecretBox().encrypt("a@example.com")))
    db.commit()
    monkeypatch.setattr("app.services.email.gmail_folders", lambda token: [
        ("INBOX", "Входящие"), ("Label_1", "Работа"),
    ])
    folders = EmailService(db).discover_folders(user, source.id)
    assert {folder.external_folder_id for folder in folders} == {"INBOX", "Label_1"}
    assert next(folder for folder in folders if folder.external_folder_id == "Label_1").is_selected is False
    label = db.scalar(select(SourceFolder).where(SourceFolder.external_folder_id == "Label_1"))
    text, markup = handle_action(db, user, SimpleNamespace(
        action="folder_toggle", payload={"id": str(source.id), "folder": "Label_1", "page": 0},
    ))
    assert "папки" in text and markup["inline_keyboard"]
    assert label.is_selected is True


def test_inbox_card_actions_and_settings(db):
    user = UserService(db).get_or_create(5003)
    source = Source(user_id=user.id, type="telegram_chat", name="chat", status="active",
                    external_source_id="5003")
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="one",
                      message_type="text", text="Задача", received_at=datetime.now(UTC))
    db.add(message)
    db.flush()
    item = InboxItem(user_id=user.id, message_id=message.id, item_type="task_candidate",
                     title="Задача")
    db.add(item)
    db.commit()
    _text, markup = inbox_view(db, user, item.id)
    labels = {button["text"] for row in markup["inline_keyboard"] for button in row}
    assert "Создать задачу" in labels and "Ответить" not in labels
    _text, menu = section_view(db, user, "inbox")
    assert menu["inline_keyboard"]
    settings = db.get(UserSettings, user.id)
    previous = settings.morning_briefing_enabled
    handle_action(db, user, SimpleNamespace(
        action="setting_toggle", payload={"field": "morning_briefing_enabled"},
    ))
    assert settings.morning_briefing_enabled is not previous


def test_retryable_ai_error_uses_first_policy_delay(db, monkeypatch):
    from contextlib import nullcontext

    user = UserService(db).get_or_create(5004)
    source = Source(user_id=user.id, type="telegram_chat", name="chat", status="active",
                    external_source_id="5004")
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id=str(uuid.uuid4()),
                      message_type="text", text="Задача", received_at=datetime.now(UTC),
                      raw_payload={"message": {"chat": {"id": 5004}}})
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(job)
    db.commit()
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))

    async def fail(*args):
        raise AppError("LLM_REQUEST_FAILED", "temporary", 502)

    monkeypatch.setattr(tasks.AIService, "process", fail)
    retry = Mock(side_effect=RuntimeError("retry scheduled"))
    monkeypatch.setattr(tasks.process_message, "retry", retry)
    with pytest.raises(RuntimeError, match="retry scheduled"):
        tasks.process_message.run(str(job.id))
    assert retry.call_args.kwargs["countdown"] == 30
    assert job.status == "queued" and message.processing_status == "queued"


def test_gmail_selected_labels_are_unioned_without_duplicate_messages(monkeypatch):
    from app.integrations.email import gmail_messages

    mail = EmailMessage()
    mail["From"] = "sender@example.com"
    mail["Subject"] = "Report"
    mail.set_content("Please review")
    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode().rstrip("=")
    seen_labels = []

    def response(body):
        result = Mock()
        result.json.return_value = body
        return result

    def get(url, *, params, **_kwargs):
        if url.endswith("/messages"):
            seen_labels.extend(params.get("labelIds", []))
            return response({"messages": [{"id": "same"}]})
        return response({"raw": raw, "internalDate": str(int(datetime.now(UTC).timestamp() * 1000))})

    monkeypatch.setattr("app.integrations.email.httpx.get", get)
    records = gmail_messages("token", datetime(2020, 1, 1, tzinfo=UTC), ["INBOX", "Work"])
    assert seen_labels == ["INBOX", "Work"]
    assert len(records) == 1


def test_imap_folder_discovery_ignores_nonselectable(monkeypatch):
    from app.integrations.email import imap_folders

    client = MagicMock()
    client.__enter__.return_value = client
    client.list.return_value = ("OK", [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "Work Projects"',
        b'(\\Noselect) "/" "Archive"',
    ])
    monkeypatch.setattr("app.integrations.email.imaplib.IMAP4_SSL", Mock(return_value=client))
    assert imap_folders("imap", "a@example.com", "password", oauth=False, host="imap.local") == [
        ("INBOX", "INBOX"), ("Work Projects", "Work Projects"),
    ]
    client.login.assert_called_once()


def test_bot_inbox_navigation_and_snooze(client, db, monkeypatch):
    from app.integrations.telegram.client import TelegramClient

    user = UserService(db).get_or_create(5005)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com")
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="mail",
                      message_type="email", sender_email="b@example.com", subject="Отчёт",
                      text="Подготовить", received_at=datetime.now(UTC))
    db.add(message)
    db.flush()
    item = InboxItem(user_id=user.id, message_id=message.id, item_type="request", title="Отчёт")
    db.add(item)
    db.commit()
    sent, edited = Mock(return_value={"message_id": 42}), Mock(return_value=True)
    monkeypatch.setattr(TelegramClient, "send_message", sent)
    monkeypatch.setattr(TelegramClient, "edit_message", edited)
    monkeypatch.setattr(TelegramClient, "answer_callback", lambda *args, **kwargs: None)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    def callback(update_id, token):
        return client.post("/webhooks/telegram", headers=headers, json={
            "update_id": update_id, "callback_query": {
                "id": str(update_id), "from": {"id": 5005}, "data": token,
                "message": {"message_id": 42, "chat": {"id": 5005, "type": "private"}},
            },
        })

    start = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 50051, "message": {
            "message_id": 1, "from": {"id": 5005},
            "chat": {"id": 5005, "type": "private"}, "text": "/start",
        },
    })
    assert start.status_code == 200
    menu = sent.call_args.args[2]["inline_keyboard"]
    inbox_token = next(button["callback_data"] for row in menu for button in row if button["text"] == "Inbox")
    assert callback(50052, inbox_token).status_code == 200
    inbox_token = sent.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
    assert callback(50053, inbox_token).status_code == 200
    card = edited.call_args.args[3]["inline_keyboard"]
    labels = {button["text"] for row in card for button in row}
    assert "Ответить" in labels and "Отложить на день" in labels
    snooze_token = next(button["callback_data"] for row in card for button in row if button["text"] == "Отложить на день")
    assert callback(50054, snooze_token).status_code == 200
    assert item.status == "snoozed" and item.snoozed_until is not None


def test_task_project_source_cards_and_actions(db):
    user = UserService(db).get_or_create(5006)
    task, _ = TaskService(db).create(user, {"title": "Отчёт"})
    project = ProjectService(db).create(user, {"name": "Работа"})
    source = SourceService(db).create(user, {
        "type": "telegram_chat", "name": "Chat", "external_source_id": "5006",
    })
    for section in ("tasks", "projects", "sources"):
        text, markup = section_view(db, user, section)
        assert text and markup["inline_keyboard"]
    for section, object_id in (("tasks", task.id), ("projects", project.id), ("sources", source.id)):
        text, markup = object_view(db, user, section, object_id)
        assert text and markup["inline_keyboard"]
    def act(kind, target):
        return SimpleNamespace(action=kind, payload={"id": str(target)})
    text, markup = handle_action(db, user, act("task_complete", task.id))
    assert task.status == "completed" and markup["inline_keyboard"]
    text, markup = handle_action(db, user, act("task_delete_prompt", task.id))
    assert "Удалить" in text and markup["inline_keyboard"]
    handle_action(db, user, act("task_delete", task.id))
    assert task.deleted_at is not None
    handle_action(db, user, act("project_archive", project.id))
    assert project.is_archived is True
    handle_action(db, user, act("source_toggle", source.id))
    assert source.status == "paused"
    handle_action(db, user, act("source_toggle", source.id))
    assert source.status == "active"
    text, markup = handle_action(db, user, act("source_disconnect_prompt", source.id))
    assert "Отключить" in text and markup["inline_keyboard"]
    handle_action(db, user, act("source_disconnect", source.id))
    assert source.status == "disconnected"


def test_default_project_setting_applies_to_new_tasks(db):
    user = UserService(db).get_or_create(5014)
    project = ProjectService(db).create(user, {"name": "Работа"})
    text, markup = handle_action(db, user, SimpleNamespace(
        action="setting_projects", payload={},
    ))
    assert "новые задачи" in text.lower() and len(markup["inline_keyboard"]) == 2
    handle_action(db, user, SimpleNamespace(
        action="setting_project", payload={"id": str(project.id)},
    ))
    task, _ = TaskService(db).create(user, {"title": "Новая задача"})
    assert task.project_id == project.id
    ProjectService(db).patch(user, project.id, {"is_archived": True})
    assert db.get(UserSettings, user.id).default_project_id is None


def test_inbox_card_create_reply_resolve_and_ignore(db):
    user = UserService(db).get_or_create(5007)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com")
    db.add(source)
    db.flush()
    items = []
    for number in range(3):
        message = Message(user_id=user.id, source_id=source.id, external_message_id=str(number),
                          message_type="email", sender_email="b@example.com", subject="Отчёт",
                          text="Подготовить", received_at=datetime.now(UTC))
        db.add(message)
        db.flush()
        item = InboxItem(user_id=user.id, message_id=message.id, item_type="request", title="Отчёт")
        db.add(item)
        items.append(item)
    db.commit()
    def action(item, operation):
        return SimpleNamespace(action="inbox_action", payload={
            "id": str(item.id), "operation": operation,
        })
    text, _ = handle_action(db, user, action(items[0], "reply"))
    assert "/reply" in text
    text, markup = handle_action(db, user, action(items[0], "create_task"))
    assert items[0].status == "resolved" and markup["inline_keyboard"]
    handle_action(db, user, action(items[1], "resolve"))
    handle_action(db, user, action(items[2], "ignore"))
    assert items[1].status == "resolved" and items[2].status == "ignored"


def test_failed_token_refresh_marks_mail_source_error(db, monkeypatch):
    from datetime import timedelta

    from app.models.entities import Notification

    user = UserService(db).get_or_create(5008)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com", connected_at=datetime.now(UTC))
    db.add(source)
    db.flush()
    db.add(SourceCredential(source_id=source.id, provider="gmail",
                            encrypted_access_token=SecretBox().encrypt("expired"),
                            encrypted_username=SecretBox().encrypt("a@example.com"),
                            token_expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()

    def fail(*args):
        raise AppError("MAIL_REAUTH_REQUIRED", "Reconnect", 401)

    monkeypatch.setattr("app.services.oauth.OAuthService.refresh_source", fail)
    with pytest.raises(AppError):
        EmailService(db).sync(user, source.id)
    assert source.status == "error"
    assert db.scalar(select(Notification).where(Notification.user_id == user.id)).type == "source_error"


def test_bot_clarification_keeps_original_email_and_queues_new_job(client, db, monkeypatch):
    from app.integrations.telegram.client import TelegramClient

    user = UserService(db).get_or_create(5012)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com")
    db.add(source)
    db.flush()
    original = Message(user_id=user.id, source_id=source.id, external_message_id="mail",
                       message_type="email", subject="Вопрос", text="Сделать это?",
                       received_at=datetime.now(UTC))
    db.add(original)
    db.commit()
    sent = Mock(return_value={"message_id": 991})
    delayed = Mock()
    monkeypatch.setattr(TelegramClient, "send_message", sent)
    monkeypatch.setattr(tasks.process_message, "delay", delayed)
    response = client.post("/webhooks/telegram", headers={
        "X-Telegram-Bot-Api-Secret-Token": "change-me",
    }, json={"update_id": 50121, "message": {
        "message_id": 201, "from": {"id": 5012},
        "chat": {"id": 5012, "type": "private"},
        "text": f"/clarify {original.id} Подготовить отчёт",
    }})
    assert response.status_code == 200 and response.json()["clarification"] is True
    assert original.text == "Сделать это?"
    refined = db.scalar(select(Message).where(Message.external_message_id == "201"))
    assert "Подготовить отчёт" in refined.text and str(original.id) in refined.text
    delayed.assert_called_once()


def test_mail_worker_retries_transient_provider_error(db, monkeypatch):
    from contextlib import nullcontext

    user = UserService(db).get_or_create(5013)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com")
    db.add(source)
    db.commit()
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))

    def fail(*args):
        raise AppError("MAIL_PROVIDER_ERROR", "temporary", 502)

    monkeypatch.setattr(tasks.EmailService, "sync", fail)
    retry = Mock(side_effect=RuntimeError("mail retry scheduled"))
    monkeypatch.setattr(tasks.sync_mail_source, "retry", retry)
    with pytest.raises(RuntimeError, match="mail retry scheduled"):
        tasks.sync_mail_source.run(str(source.id))
    assert retry.call_args.kwargs["countdown"] == 30
    assert source.status == "active"


def test_scheduler_queues_active_mailboxes(db, monkeypatch):
    from contextlib import nullcontext

    user = UserService(db).get_or_create(5015)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com")
    db.add(source)
    db.commit()
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))
    delayed = Mock()
    monkeypatch.setattr(tasks.sync_mail_source, "delay", delayed)
    result = tasks.schedule_tick.run()
    assert result["mail"] == {"sources": 1, "queued": 1}
    delayed.assert_called_once_with(str(source.id))


def test_email_import_analysis_reaches_inbox(db, monkeypatch):
    from contextlib import nullcontext
    from datetime import timedelta

    from app.integrations.email import MailRecord
    from app.services.analysis import queue_source_analysis

    user = UserService(db).get_or_create(5016)
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="a@example.com",
                    connected_at=datetime.now(UTC) - timedelta(days=1),
                    last_synced_at=datetime.now(UTC) - timedelta(days=1))
    db.add(source)
    db.flush()
    db.add(SourceCredential(source_id=source.id, provider="gmail",
                            encrypted_access_token=SecretBox().encrypt("token"),
                            encrypted_username=SecretBox().encrypt("a@example.com")))
    db.add(SourceFolder(source_id=source.id, external_folder_id="INBOX",
                        name="Входящие", is_selected=True))
    db.commit()
    record = MailRecord(external_id="new-mail", thread_id=None,
                        sender="sender@example.com", sender_name="Sender", subject="Поручение",
                        text="Создай задачу подготовить отчёт", received_at=datetime.now(UTC),
                        message_id_header="<new-mail@example.com>")
    monkeypatch.setattr("app.services.email.gmail_messages", Mock(return_value=[record]))
    assert EmailService(db).sync(user, source.id) == 1
    delayed = Mock()
    monkeypatch.setattr(tasks.process_message, "delay", delayed)
    assert queue_source_analysis(db, user, source.id, sync_external=False)["queued"] == 1
    job = db.scalar(select(AIProcessingJob).where(AIProcessingJob.user_id == user.id))
    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))
    outcome = tasks.process_message.run(str(job.id))
    item = db.scalar(select(InboxItem).where(InboxItem.user_id == user.id))
    assert outcome["state"] == "proposal" and item.item_type == "task_candidate"


def test_task_events_service_and_api(db, client):
    from app.core.security import issue_user_token

    user = UserService(db).get_or_create(5017)
    token = issue_user_token(5017)
    headers = {"Authorization": f"Bearer {token}"}

    task, _ = TaskService(db).create(user, {"title": "Initial Task"})
    TaskService(db).patch(user, task.id, {"title": "Updated Task"})
    TaskService(db).change_status(user, task.id, "in_progress")

    events = TaskService(db).events(user, task.id)
    assert len(events) >= 2
    assert events[0].event_type == "created"

    response = client.get(f"/api/v1/tasks/{task.id}/events", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["data"]) >= 2
    assert data["data"][0]["event_type"] == "created"


def test_wake_due_snoozed_and_inbox_filtering(db):
    from datetime import timedelta

    from app.bot.presentation import section_message
    from app.services.domain import InboxService, utcnow

    user = UserService(db).get_or_create(5018)
    inbox_service = InboxService(db)

    past_due = utcnow() - timedelta(hours=2)
    item_past = InboxItem(user_id=user.id, title="Past Item", item_type="task_candidate", status="snoozed", snoozed_until=past_due)
    db.add(item_past)

    future_due = utcnow() + timedelta(hours=5)
    item_future = InboxItem(user_id=user.id, title="Future Item", item_type="task_candidate", status="snoozed", snoozed_until=future_due)
    db.add(item_future)
    db.commit()

    msg = section_message(db, user, "inbox")
    text, markup = section_view(db, user, "inbox")
    assert "Future Item" not in msg
    assert "Future Item" not in text

    woken = inbox_service.wake_due_snoozed()
    assert woken >= 1

    db.refresh(item_past)
    assert item_past.status == "proposed"
    assert item_past.snoozed_until is None


def test_task_duplicate_allowed_after_completed_or_cancelled(db):
    user = UserService(db).get_or_create(5019)
    source = SourceService(db).create(user, {"type": "telegram_chat", "name": "Chat", "external_source_id": "5019"})
    message = Message(user_id=user.id, source_id=source.id, external_message_id="msg-dup", message_type="text", text="Hello")
    db.add(message)
    db.commit()

    task1, _ = TaskService(db).create(user, {"title": "First Task", "source_message_id": message.id})
    with pytest.raises(AppError) as exc_info:
        TaskService(db).create(user, {"title": "Second Task", "source_message_id": message.id})
    assert exc_info.value.code == "DUPLICATE_TASK"

    TaskService(db).change_status(user, task1.id, "completed")

    task2, _ = TaskService(db).create(user, {"title": "Recreated Task", "source_message_id": message.id})
    assert task2.id != task1.id
    assert task2.title == "Recreated Task"


def test_bot_slash_commands_and_short_token_resolution(db, client, monkeypatch):
    from app.core.config import get_settings
    from app.integrations.telegram.client import TelegramClient

    sent = Mock(return_value={"message_id": 991})
    delayed = Mock()
    monkeypatch.setattr(TelegramClient, "send_message", sent)
    monkeypatch.setattr(tasks.process_message, "delay", delayed)

    user = UserService(db).get_or_create(5020)
    task, _ = TaskService(db).create(user, {"title": "Old Task Name"})
    project = ProjectService(db).create(user, {"name": "Old Project Name"})
    source = SourceService(db).create(user, {"type": "telegram_chat", "name": "Chat", "external_source_id": "5020"})
    msg = Message(user_id=user.id, source_id=source.id, external_message_id="msg-clarify", message_type="text", text="Help needed")
    db.add(msg)
    db.commit()

    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    def cmd(text):
        return client.post("/webhooks/telegram", headers=headers, json={
            "update_id": uuid.uuid4().int % 10000000,
            "message": {
                "message_id": 1,
                "from": {"id": 5020, "first_name": "Test"},
                "chat": {"id": 5020, "type": "private"},
                "text": text,
            }
        })

    for slash in ("/tasks", "/projects", "/inbox", "/sources", "/settings"):
        res = cmd(slash)
        assert res.status_code == 200
        assert res.json()["command"] == slash.removeprefix("/")

    token = UIActionService(db).create(user, "task_edit_target", {"id": str(task.id)})
    res = cmd(f"/task_edit {token} New Brand Title")
    assert res.status_code == 200
    db.refresh(task)
    assert task.title == "New Brand Title"

    prefix = str(task.id)[:8]
    res = cmd(f"/task_edit {prefix} Prefix Brand Title")
    assert res.status_code == 200
    db.refresh(task)
    assert task.title == "Prefix Brand Title"

    p_token = UIActionService(db).create(user, "project_rename_target", {"id": str(project.id)})
    res = cmd(f"/project_rename {p_token} Renamed Project")
    assert res.status_code == 200
    db.refresh(project)
    assert project.name == "Renamed Project"

    c_token = UIActionService(db).create(user, "clarify_target", {"message_id": str(msg.id)})
    res = cmd(f"/clarify {c_token} more details")
    assert res.status_code == 200
    assert res.json()["clarification"] is True


def test_bot_actions_snooze_calendar_source_waiting_reminder(db):
    from datetime import timedelta

    from app.models.entities import CalendarConnection, CalendarEvent
    from app.services.domain import ReminderService, WaitingService, utcnow

    user = UserService(db).get_or_create(5021)
    task, _ = TaskService(db).create(user, {"title": "Task for Snooze", "due_at": utcnow()})
    source = SourceService(db).create(user, {"type": "telegram_chat", "name": "Chat", "external_source_id": "5021"})

    def act(kind, payload):
        return SimpleNamespace(action=kind, payload=payload)

    orig_due = task.due_at.replace(tzinfo=None) if task.due_at.tzinfo else task.due_at
    text, markup = handle_action(db, user, act("task_snooze", {"id": str(task.id)}))
    db.refresh(task)
    task_due = task.due_at.replace(tzinfo=None) if task.due_at.tzinfo else task.due_at
    assert task_due > orig_due
    assert markup["inline_keyboard"][0][0]["text"] == "Отменить"

    cal_conn = CalendarConnection(user_id=user.id, provider="google", status="active", external_account_id="u@example.com")
    db.add(cal_conn)
    db.commit()
    text, _ = handle_action(db, user, act("task_add_calendar", {"task_id": str(task.id)}))
    assert "добавлено" in text
    event = db.scalar(select(CalendarEvent).where(CalendarEvent.task_id == task.id))
    assert event is not None
    assert event.title == task.title

    assert source.analysis_text is True
    handle_action(db, user, act("source_setting_toggle", {"id": str(source.id), "field": "analysis_text"}))
    db.refresh(source)
    assert source.analysis_text is False

    waiting = WaitingService(db).create(user, {"title": "Waiting for document"})
    text, markup = handle_action(db, user, act("waiting_open", {"id": str(waiting.id)}))
    assert "Ожидание" in text and markup["inline_keyboard"]
    handle_action(db, user, act("waiting_complete", {"id": str(waiting.id)}))
    db.refresh(waiting)
    assert waiting.status == "completed"
    handle_action(db, user, act("waiting_cancel", {"id": str(waiting.id)}))
    db.refresh(waiting)
    assert waiting.status == "cancelled"

    rem = ReminderService(db).create(user, {"title": "Reminder 1", "due_at": utcnow() + timedelta(hours=1)})
    handle_action(db, user, act("reminder_cancel", {"id": str(rem.id)}))
    db.refresh(rem)
    assert rem.status == "cancelled"


def test_notification_deliver_pending_markups(db):
    from app.models.entities import Notification
    from app.services.domain import NotificationService

    user = UserService(db).get_or_create(5022)
    service = NotificationService(db)

    db.add(Notification(user_id=user.id, type="morning_briefing", payload={"title": "Morning"}))
    db.add(Notification(user_id=user.id, type="evening_stats", payload={"title": "Evening"}))
    inbox_item = InboxItem(user_id=user.id, title="Urgent Invoice", item_type="urgent")
    db.add(inbox_item)
    db.add(Notification(user_id=user.id, type="urgent_item", payload={"title": "🔴 Срочно", "message_id": None}))
    db.commit()

    client = Mock()
    client.send_message.return_value = {"message_id": 999}
    sent_count = service.deliver_pending(client)
    assert sent_count == 3
    assert client.send_message.call_count == 3

    markups = [call.args[2] for call in client.send_message.call_args_list if len(call.args) > 2 and call.args[2]]
    button_labels = [btn["text"] for m in markups for row in m["inline_keyboard"] for btn in row]
    assert "Открыть задачи" in button_labels
    assert "Расписание" in button_labels
    assert "Задачи на завтра" in button_labels
    assert "Настройки" in button_labels
    assert "В Inbox" in button_labels


def test_voice_message_processing_echo_and_calendar_suggest(db, monkeypatch):
    from contextlib import nullcontext

    from app.models.entities import CalendarConnection

    user = UserService(db).get_or_create(5023)
    source = SourceService(db).create(user, {"type": "telegram_chat", "name": "Chat", "external_source_id": "5023"})
    message = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="v-1",
        message_type="voice",
        text="Купить молоко в 18:00",
        raw_payload={"message": {"chat": {"id": 5023}}},
    )
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id, status="queued")
    db.add(job)

    cal = CalendarConnection(user_id=user.id, provider="google", status="active", external_account_id="c@example.com")
    db.add(cal)
    db.commit()

    monkeypatch.setattr(tasks, "SessionLocal", lambda: nullcontext(db))
    sent_messages = []
    fake_client = Mock()
    fake_client.send_message.side_effect = lambda chat_id, text, markup=None: sent_messages.append((text, markup)) or {"message_id": 1}
    fake_client.edit_message.side_effect = lambda chat_id, msg_id, text, markup=None: sent_messages.append((text, markup)) or {"message_id": 1}
    monkeypatch.setattr("app.workers.tasks.TelegramClient", lambda: fake_client)

    tasks.process_message.run(str(job.id))
    assert len(sent_messages) >= 1
    sent_text, markup = sent_messages[0]
    assert "🎤 Я понял: «Купить молоко в 18:00»" in sent_text



def test_ai_task_respects_default_project_id_when_source_has_no_project(db):
    user = UserService(db).get_or_create(5030)
    proj = ProjectService(db).create(user, {"name": "Default Proj"})
    settings = db.get(UserSettings, user.id)
    settings.default_project_id = proj.id
    db.commit()

    # 1. Direct TaskService.create with project_id: None or missing
    task1, _ = TaskService(db).create(user, {"title": "Task 1", "project_id": None})
    assert task1.project_id == proj.id

    task2, _ = TaskService(db).create(user, {"title": "Task 2"})
    assert task2.project_id == proj.id

    # 2. AIService._interpret_source_message with no linked source projects
    source = Source(user_id=user.id, type="gmail", name="mail", status="active",
                    external_source_id="b@example.com", connected_at=datetime.now(UTC))
    db.add(source)
    db.flush()
    message = Message(user_id=user.id, source_id=source.id, external_message_id="msg-no-proj",
                      message_type="email", subject="Test", text="Do this", received_at=datetime.now(UTC))
    db.add(message)
    db.commit()

    class DummyProvider:
        async def interpret(self, text):
            return AIResult(intent="create_task", confidence=0.9, entities={"title": "Test Task"},
                            action=AIAction(type="create_task"), reason="Поручение")

    ai_service = AIService(db, DummyProvider())
    job = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(job)
    db.commit()

    outcome = asyncio.run(ai_service.process(job, message, user))
    assert outcome["state"] == "proposal"
    action = UIActionService(db).consume(user, outcome["token"], "confirm_ai")
    assert action.payload["result"]["entities"]["project_id"] == str(proj.id)
    executed = AIActionService(db).execute(user, AIResult.model_validate(action.payload["result"]), message)
    task = db.get(Task, uuid.UUID(executed["object_id"]))
    assert task.project_id == proj.id


def test_analyze_without_sources_provides_connect_button(db, client):
    from app.core.config import get_settings
    user = UserService(db).get_or_create(5031)
    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    res = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 7001,
        "message": {
            "message_id": 1,
            "from": {"id": 5031, "first_name": "Test"},
            "chat": {"id": 5031, "type": "private"},
            "text": "/analyze",
        }
    })
    assert res.status_code == 200
    assert res.json()["command"] == "analyze"

    # Verify section_view sources has connect button and settings has timezone button
    text_src, markup_src = section_view(db, user, "sources")
    labels_src = [b["text"] for row in markup_src["inline_keyboard"] for b in row]
    assert "Подключить источник" in labels_src

    # handle_action with source_connect_menu
    t_conn, m_conn = handle_action(db, user, SimpleNamespace(action="source_connect_menu", payload={}))
    assert "Подключить почту" in t_conn
    assert m_conn and "inline_keyboard" in m_conn

    # Check settings view
    text_set, markup_set = section_view(db, user, "settings")
    labels_set = [b["text"] for row in markup_set["inline_keyboard"] for b in row]
    assert "Изменить часовой пояс" in labels_set

    # handle_action with setting_timezone
    t_tz, m_tz = handle_action(db, user, SimpleNamespace(action="setting_timezone", payload={}))
    assert "/timezone" in t_tz


def test_disambiguation_on_ambiguous_prefix_edit_and_rename(db, client):
    from app.core.config import get_settings
    user = UserService(db).get_or_create(5032)
    id_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    id_b = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
    task_a = Task(id=id_a, user_id=user.id, title="Task Alpha")
    task_b = Task(id=id_b, user_id=user.id, title="Task Beta")
    db.add(task_a)
    db.add(task_b)
    db.commit()

    prefix = "aaaaaaaa"
    secret = get_settings().telegram_webhook_secret
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    # /task_edit with ambiguous prefix
    res = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 7002,
        "message": {
            "message_id": 2,
            "from": {"id": 5032, "first_name": "Test"},
            "chat": {"id": 5032, "type": "private"},
            "text": f"/task_edit {prefix} Renamed Both",
        }
    })
    assert res.status_code == 200
    assert res.json().get("ambiguity") is True

    # Test executing disambiguated edit action
    t_out, m_out = handle_action(db, user, SimpleNamespace(
        action="task_disambiguate_edit",
        payload={"id": str(task_a.id), "title": "Renamed Alpha Solo"}
    ))
    db.refresh(task_a)
    assert task_a.title == "Renamed Alpha Solo"

    # Test project disambiguation
    pid_a = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
    pid_b = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
    proj_a = Project(id=pid_a, user_id=user.id, name="Project Alpha")
    proj_b = Project(id=pid_b, user_id=user.id, name="Project Beta")
    db.add(proj_a)
    db.add(proj_b)
    db.commit()
    p_prefix = "bbbbbbbb"

    res_p = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 7003,
        "message": {
            "message_id": 3,
            "from": {"id": 5032, "first_name": "Test"},
            "chat": {"id": 5032, "type": "private"},
            "text": f"/project_rename {p_prefix} Renamed Proj Ambiguous",
        }
    })
    assert res_p.status_code == 200
    assert res_p.json().get("ambiguity") is True

    # Execute disambiguated rename
    t_p, _ = handle_action(db, user, SimpleNamespace(
        action="project_disambiguate_rename",
        payload={"id": str(proj_a.id), "name": "Project Alpha Solo"}
    ))
    db.refresh(proj_a)
    assert proj_a.name == "Project Alpha Solo"

    # Cancel AI action
    t_c, _ = handle_action(db, user, SimpleNamespace(action="cancel_ai", payload={}))
    assert "отменено" in t_c


def test_telegram_mode_config_and_validation():
    from app.core.config import Settings
    s_default = Settings()
    assert s_default.telegram_mode == "polling"

    # In production with polling mode, TELEGRAM_WEBHOOK_SECRET is not required
    s_prod_polling = Settings(
        app_env="production",
        internal_api_token="valid-token-123",
        encryption_key="valid-key-123",
        telegram_bot_token="bot123:token",
        telegram_mode="polling",
        telegram_webhook_secret="change-me",
    )
    assert s_prod_polling.telegram_mode == "polling"

    # In production with webhook mode, TELEGRAM_WEBHOOK_SECRET is required
    with pytest.raises(ValueError) as exc:
        Settings(
            app_env="production",
            internal_api_token="valid-token-123",
            encryption_key="valid-key-123",
            telegram_bot_token="bot123:token",
            telegram_mode="webhook",
            telegram_webhook_secret="change-me",
        )
    assert "TELEGRAM_WEBHOOK_SECRET" in str(exc.value)


def test_telegram_client_webhook_and_polling_methods(monkeypatch):
    from app.integrations.telegram.client import TelegramClient
    tc = TelegramClient(token="mock-token")
    mock_post = Mock()
    mock_post.return_value.json.return_value = {"ok": True, "result": [{"update_id": 100, "message": {"text": "hi"}}]}
    mock_post.return_value.raise_for_status = Mock()
    monkeypatch.setattr("app.integrations.telegram.client.httpx.post", mock_post)

    # get_updates
    updates = tc.get_updates(offset=99, timeout=10)
    assert len(updates) == 1 and updates[0]["update_id"] == 100

    # delete_webhook
    mock_post.return_value.json.return_value = {"ok": True, "result": True}
    assert tc.delete_webhook() is True

    # set_webhook
    assert tc.set_webhook("https://example.com/wh", secret_token="sec123") is True


def test_polling_runner_and_poll_once(db, monkeypatch):
    import threading

    from app.bot.polling import poll_once, run_polling
    from app.integrations.telegram.client import TelegramClient

    monkeypatch.setattr(TelegramClient, "send_message", Mock(return_value={"message_id": 991}))
    monkeypatch.setattr(TelegramClient, "delete_webhook", Mock(return_value=True))
    monkeypatch.setattr(tasks.process_message, "delay", Mock())

    client = Mock()
    client.token = "test-token"
    client.get_updates.return_value = [
        {"update_id": 8001, "message": {"message_id": 1, "chat": {"id": 8001, "type": "private"}, "from": {"id": 8001}, "text": "/today"}}
    ]
    client.delete_webhook.return_value = True

    new_offset, results = poll_once(client, db, offset=8000, timeout=5)
    assert new_offset == 8002
    assert len(results) == 1

    # run_polling with stop_event
    stop_ev = threading.Event()
    stop_ev.set()
    run_polling(stop_event=stop_ev, max_iterations=1)
