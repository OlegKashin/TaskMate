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
    Source,
    SourceCredential,
    SourceFolder,
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
    previous = settings.weather_enabled
    handle_action(db, user, SimpleNamespace(
        action="setting_toggle", payload={"field": "weather_enabled"},
    ))
    assert settings.weather_enabled is not previous


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
