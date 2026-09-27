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


def test_user_service_patch_timezone_validation(db):
    user = UserService(db).get_or_create(5020)
    with pytest.raises(AppError) as exc_info:
        UserService(db).patch(user, {"timezone": "Invalid/Timezone_Name"})
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert exc_info.value.status_code == 422

    user = UserService(db).patch(user, {"timezone": "Europe/Moscow"})
    assert user.timezone == "Europe/Moscow"


def test_stats_api_returns_tomorrow_count(client, db, headers):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    user = UserService(db).get_or_create(1001)
    user.timezone = "Europe/Moscow"
    zone = ZoneInfo(user.timezone)
    now_local = datetime.now(zone)
    start_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_tomorrow = start_today + timedelta(days=1)
    due_tomorrow = (start_tomorrow + timedelta(hours=10)).astimezone(UTC)

    TaskService(db).create(user, {"title": "Task due tomorrow", "due_at": due_tomorrow})
    db.commit()

    res = client.get("/api/v1/stats", headers=headers)
    assert res.status_code == 200
    data = res.json()["data"]
    assert "tomorrow" in data
    assert data["tomorrow"] >= 1


def test_proposal_button_order_per_tz(db):
    user = UserService(db).get_or_create(5021)
    # Medium confidence proposal: [Изменить] [Подтвердить] [Отмена]
    text, markup = outcome_message(
        db, user,
        {"state": "proposal", "intent": "create_task", "confidence": "medium", "proposal": {"title": "Задача"}, "token": "tok1"}
    )
    buttons = [b["text"] for b in markup["inline_keyboard"][0]]
    assert buttons == ["Изменить", "Подтвердить", "Отмена"]

    # High confidence proposal: [Подтвердить] [Отмена]
    text, markup = outcome_message(
        db, user,
        {"state": "proposal", "intent": "create_task", "confidence": "high", "proposal": {"title": "Задача"}, "token": "tok2"}
    )
    buttons = [b["text"] for b in markup["inline_keyboard"][0]]
    assert buttons == ["Подтвердить", "Отмена"]

    # Warnings for project deletion and source disconnect in proposal
    t_proj, _ = outcome_message(
        db, user,
        {"state": "proposal", "intent": "project_action", "confidence": "high", "entities": {"action": "delete"}, "token": "tok3"}
    )
    assert "без возможности Undo" in t_proj

    t_src, _ = outcome_message(
        db, user,
        {"state": "proposal", "intent": "source_action", "confidence": "high", "entities": {"action": "disconnect"}, "token": "tok4"}
    )
    assert "Источник будет отключён" in t_src


def test_telegram_help_command(db):
    from app.bot.telegram import handle_telegram_command
    user = UserService(db).get_or_create(5022)
    res = handle_telegram_command(db, user, "/help", 5022)
    assert res.ok is True
    assert res.command == "help"
    assert "Разделы" in res.text
    assert "/tasks" in res.text
    assert "/today" in res.text
    assert "/settings" in res.text


def test_all_new_actions_in_actions_set():
    from app.bot.actions import ACTIONS
    expected = {
        "cancel_ai", "edit_ai", "task_snooze", "task_add_calendar",
        "source_setting_toggle", "waiting_open", "waiting_complete",
        "waiting_cancel", "reminder_cancel",
    }
    assert expected.issubset(ACTIONS)


def test_action_handlers_extended(db):
    from datetime import timedelta
    from app.models.entities import CalendarConnection, CalendarEvent, WaitingItem, Reminder, Project
    from app.services.domain import CalendarService, WaitingService, ReminderService, ProjectService

    user = UserService(db).get_or_create(5023)

    # 1. cancel_ai & edit_ai
    t1, m1 = handle_action(db, user, SimpleNamespace(action="cancel_ai", payload={}))
    assert t1 == "Действие отменено." and m1 is None
    t2, m2 = handle_action(db, user, SimpleNamespace(action="edit_ai", payload={}))
    assert "Отправьте уточнённое сообщение" in t2 and m2 is None

    # 2. task_new_prompt & project_new_prompt
    t_np, _ = handle_action(db, user, SimpleNamespace(action="task_new_prompt", payload={}))
    assert "/task" in t_np
    t_pnp, _ = handle_action(db, user, SimpleNamespace(action="project_new_prompt", payload={}))
    assert "/project" in t_pnp

    # 3. task_snooze
    task, _ = TaskService(db).create(user, {"title": "Snooze me"})
    t3, m3 = handle_action(db, user, SimpleNamespace(action="task_snooze", payload={"id": str(task.id)}))
    assert task.due_at is not None

    # 4. task_add_calendar without connection
    t4, m4 = handle_action(db, user, SimpleNamespace(action="task_add_calendar", payload={"task_id": str(task.id)}))
    assert "не найдено" in t4

    # 5. task_add_calendar with connection
    conn = CalendarConnection(user_id=user.id, provider="google", status="active", external_account_id="user@gmail.com")
    db.add(conn)
    db.commit()
    t5, m5 = handle_action(db, user, SimpleNamespace(action="task_add_calendar", payload={"task_id": str(task.id)}))
    assert "добавлено в Google Calendar" in t5

    # 6. source_setting_toggle
    source = SourceService(db).create(user, {"type": "telegram_chat", "name": "Src", "external_source_id": "5023"})
    prev_val = source.analysis_text
    handle_action(db, user, SimpleNamespace(action="source_setting_toggle", payload={"id": str(source.id), "field": "analysis_text"}))
    assert source.analysis_text is not prev_val

    # 7. waiting_open, waiting_complete, waiting_cancel
    wait_item = WaitingService(db).create(user, {"title": "Жду ответ", "counterparty": "Коллега"})
    t6, m6 = handle_action(db, user, SimpleNamespace(action="waiting_open", payload={"id": str(wait_item.id)}))
    assert "Ожидание: Жду ответ" in t6
    t7, m7 = handle_action(db, user, SimpleNamespace(action="waiting_complete", payload={"id": str(wait_item.id)}))
    assert "завершённым" in t7 and wait_item.status == "completed"
    wait_item2 = WaitingService(db).create(user, {"title": "Жду еще", "counterparty": "Друг"})
    t8, m8 = handle_action(db, user, SimpleNamespace(action="waiting_cancel", payload={"id": str(wait_item2.id)}))
    assert "отменено" in t8 and wait_item2.status == "cancelled"

    # 8. reminder_cancel
    rem = ReminderService(db).create(user, {"title": "Напомни", "due_at": datetime.now(UTC) + timedelta(hours=1)})
    t9, m9 = handle_action(db, user, SimpleNamespace(action="reminder_cancel", payload={"id": str(rem.id)}))
    assert "Напоминание отменено" in t9 and rem.status == "cancelled"

    # 9. task_complete_prompt, project_delete_prompt, project_delete, project_rename_prompt
    t10, m10 = handle_action(db, user, SimpleNamespace(action="task_complete_prompt", payload={"id": str(task.id)}))
    assert "Отметить задачу" in t10
    proj = ProjectService(db).create(user, {"name": "Проект Дел"})
    t11, m11 = handle_action(db, user, SimpleNamespace(action="project_rename_prompt", payload={"id": str(proj.id)}))
    assert "/project_rename" in t11
    t12, m12 = handle_action(db, user, SimpleNamespace(action="project_delete_prompt", payload={"id": str(proj.id)}))
    assert "Удалить проект" in t12
    t13, m13 = handle_action(db, user, SimpleNamespace(action="project_delete", payload={"id": str(proj.id)}))
    assert "удалён" in t13

    # 10. settings view has no weather
    t14, m14 = section_view(db, user, "settings")
    assert "Погода" not in t14
    for row in m14.get("inline_keyboard", []):
        for btn in row:
            assert "Погода" not in btn["text"]

    # 11. setting_times, setting_projects, setting_project
    t15, _ = handle_action(db, user, SimpleNamespace(action="setting_times", payload={}))
    assert "/briefing_time" in t15
    t16, m16 = handle_action(db, user, SimpleNamespace(action="setting_projects", payload={}))
    assert "новые задачи" in t16
    t17, _ = handle_action(db, user, SimpleNamespace(action="setting_project", payload={"id": None}))
    assert "Настройки" in t17

    # 12. calendar_cancel_prompt and calendar_cancel
    cal_ev = CalendarService(db).create(user, {
        "connection_id": conn.id, "title": "Встреча", "start_at": datetime.now(UTC),
        "end_at": datetime.now(UTC) + timedelta(hours=1),
    })
    t18, m18 = handle_action(db, user, SimpleNamespace(action="calendar_cancel_prompt", payload={"id": str(cal_ev.id)}))
    assert "Отменить событие" in t18
    t19, _ = handle_action(db, user, SimpleNamespace(action="calendar_cancel", payload={"id": str(cal_ev.id)}))
    assert "отменено" in t19
    t20, _ = handle_action(db, user, SimpleNamespace(action="calendar_cancel_prompt", payload={"id": str(cal_ev.id)}))
    assert "уже отменено" in t20

    # 13. inbox_action clarify without message
    item = InboxItem(user_id=user.id, item_type="note", title="Заметка без письма")
    db.add(item)
    db.commit()
    t21, _ = handle_action(db, user, SimpleNamespace(action="inbox_action", payload={"id": str(item.id), "operation": "clarify"}))
    assert "недоступно" in t21
    t22, _ = handle_action(db, user, SimpleNamespace(action="inbox_action", payload={"id": str(item.id), "operation": "reply"}))
    assert "только для письма" in t22


def test_telegram_commands_and_callbacks(client, db, monkeypatch):
    from app.integrations.telegram.client import TelegramClient
    sent = Mock(return_value={"message_id": 99})
    edited = Mock(return_value=True)
    monkeypatch.setattr(TelegramClient, "send_message", sent)
    monkeypatch.setattr(TelegramClient, "edit_message", edited)
    monkeypatch.setattr(TelegramClient, "answer_callback", Mock())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    # /help via webhook
    res = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6000, "message": {
            "message_id": 0, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/help",
        },
    })
    assert res.status_code == 200
    assert "Разделы" in sent.call_args.args[1]

    # /timezone valid & invalid
    res = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6001, "message": {
            "message_id": 1, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/timezone Europe/Berlin",
        },
    })
    assert res.status_code == 200
    assert "Europe/Berlin" in sent.call_args.args[1]

    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6002, "message": {
            "message_id": 2, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/timezone Bad/Zone",
        },
    })
    assert "Неизвестный часовой пояс" in sent.call_args.args[1]

    # /briefing_time & /stats_time
    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6003, "message": {
            "message_id": 3, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/briefing_time 09:30",
        },
    })
    assert "09:30" in sent.call_args.args[1]

    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6004, "message": {
            "message_id": 4, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/briefing_time invalid",
        },
    })
    assert "Неизвестное время" in sent.call_args.args[1]

    # /connect
    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6005, "message": {
            "message_id": 5, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/connect",
        },
    })
    assert "Подключить почту" in sent.call_args.args[1]

    # /task with empty title
    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6006, "message": {
            "message_id": 6, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/task",
        },
    })
    assert "Укажите название" in sent.call_args.args[1]

    # /project with empty title
    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6007, "message": {
            "message_id": 7, "from": {"id": 6001}, "chat": {"id": 6001, "type": "private"},
            "text": "/project",
        },
    })
    assert "Укажите название проекта" in sent.call_args.args[1]

    # callbacks: cancel_ai & edit_ai
    user = UserService(db).get_or_create(6001)
    tok_cancel = UIActionService(db).create(user, "cancel_ai", {})
    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6008, "callback_query": {
            "id": "cb1", "from": {"id": 6001}, "data": tok_cancel,
            "message": {"message_id": 100, "chat": {"id": 6001, "type": "private"}},
        },
    })
    assert "отменено" in edited.call_args.args[2]

    tok_edit = UIActionService(db).create(user, "edit_ai", {})
    client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 6009, "callback_query": {
            "id": "cb2", "from": {"id": 6001}, "data": tok_edit,
            "message": {"message_id": 100, "chat": {"id": 6001, "type": "private"}},
        },
    })
    assert "исправленный запрос" in edited.call_args.args[2]


def test_telegram_client_and_storage_methods(monkeypatch):
    from app.integrations.telegram.client import TelegramClient
    from app.integrations.storage import S3Storage, initialize_local_bucket

    # TelegramClient with empty token
    tc = TelegramClient(token="")
    assert tc.send_message(123, "test") is None
    assert tc.edit_message(123, 1, "test") is None
    assert tc.answer_callback(None) is None

    # TelegramClient methods with mock post
    tc2 = TelegramClient(token="mock-token")
    mock_resp = Mock()
    mock_resp.raise_for_status = Mock()
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 10}}
    monkeypatch.setattr("app.integrations.telegram.client.httpx.post", Mock(return_value=mock_resp))
    assert tc2.send_message(123, "hi") == {"message_id": 10}
    assert tc2.edit_message(123, 10, "edit") == {"message_id": 10}
    assert tc2.answer_callback("cb-id", "ok") == {"message_id": 10}
    assert tc2.get_chat_member(123, 456) == {"message_id": 10}

    # S3Storage ensure_bucket and signed_url
    mock_s3 = Mock()
    mock_s3.list_buckets.return_value = {"Buckets": []}
    mock_s3.generate_presigned_url.return_value = "https://s3.local/signed"
    storage = S3Storage(client=mock_s3)
    storage.ensure_bucket()
    mock_s3.create_bucket.assert_called_once_with(Bucket=storage.bucket)
    assert storage.signed_url("key123") == "https://s3.local/signed"

    # initialize_local_bucket
    monkeypatch.setattr("app.integrations.storage.S3Storage", Mock(return_value=storage))
    initialize_local_bucket(attempts=1)


def test_cleanup_expired_worker(db, monkeypatch):
    from contextlib import nullcontext
    from app.workers.tasks import cleanup_expired
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: nullcontext(db))
    res = cleanup_expired.run()
    assert "ui_actions" in res and "oauth_states" in res


def test_section_message_presentation_coverage(db):
    from app.bot.presentation import section_message
    user = UserService(db).get_or_create(5025)
    user.timezone = "Bad/Timezone"
    # fallback to UTC
    msg = section_message(db, user, "schedule")
    assert "Расписание" in msg
    msg_today = section_message(db, user, "today")
    assert "Сегодня" in msg_today
