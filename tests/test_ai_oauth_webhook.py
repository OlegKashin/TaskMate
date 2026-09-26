import asyncio
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from app.ai.service import AIActionService, AIService, RuleBasedProvider
from app.core.errors import AppError
from app.core.security import SecretBox
from app.integrations.storage import S3Storage
from app.models.entities import AIProcessingJob, Message, Source
from app.schemas.domain import AIAction, AIResult
from app.services.domain import UIActionService, UserService, base36, from_base36
from app.services.oauth import OAuthService


def test_rule_provider_and_action_service(db):
    provider = RuleBasedProvider()
    assert (
        asyncio.run(provider.interpret("Создай задачу подготовить отчет")).intent == "create_task"
    )
    assert asyncio.run(provider.interpret("Напомни позвонить")).intent == "reminder"
    assert asyncio.run(provider.interpret("Жду ответ от Ивана")).intent == "create_waiting_for"
    assert asyncio.run(provider.interpret("Как дела?")).intent == "general_query"
    user = UserService(db).get_or_create(1)
    low = AIResult(
        intent="create_task",
        confidence=0.2,
        entities={"title": "x"},
        action=AIAction(type="create_task"),
        reason="unclear",
    )
    assert AIActionService(db).apply(user, low)["state"] == "clarification"
    high = AIResult(
        intent="create_task",
        confidence=0.9,
        entities={"title": "x"},
        action=AIAction(type="create_task"),
        reason="clear",
    )
    proposal = AIActionService(db).apply(user, high)
    assert proposal["state"] == "proposal" and proposal["token"].startswith("a:")


def test_ai_processing_and_failure(db):
    user = UserService(db).get_or_create(2)
    source = Source(
        user_id=user.id, type="telegram_chat", name="T", status="active", external_source_id="2"
    )
    db.add(source)
    db.flush()
    message = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="1",
        message_type="text",
        text="Как дела?",
        received_at=datetime.now(UTC),
    )
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(job)
    db.commit()
    assert asyncio.run(AIService(db).process(job, message, user))["state"] == "informational"

    class Broken:
        async def interpret(self, text):
            raise RuntimeError("boom")

    other = AIProcessingJob(user_id=user.id, message_id=message.id)
    db.add(other)
    db.commit()
    with pytest.raises(RuntimeError):
        asyncio.run(AIService(db, Broken()).process(other, message, user))
    assert other.status == "failed"


def test_voice_without_transcription_is_not_marked_processed(db):
    user = UserService(db).get_or_create(21)
    source = Source(
        user_id=user.id,
        type="telegram_chat",
        name="Voice",
        status="active",
        external_source_id="21",
    )
    db.add(source)
    db.flush()
    message = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="voice-1",
        message_type="voice",
        received_at=datetime.now(UTC),
        processing_status="queued",
    )
    db.add(message)
    db.flush()
    job = AIProcessingJob(user_id=user.id, message_id=message.id, job_type="stt")
    db.add(job)
    db.commit()
    with pytest.raises(AppError) as exc:
        asyncio.run(AIService(db).process(job, message, user))
    assert exc.value.code == "STT_NOT_CONFIGURED"
    assert job.status == "failed" and job.error_code == "STT_NOT_CONFIGURED"
    assert message.processing_status == "failed"


def test_ui_tokens_and_security(db):
    assert from_base36(base36(12345)) == 12345
    with pytest.raises(ValueError):
        base36(-1)
    user = UserService(db).get_or_create(3)
    token = UIActionService(db).create(user, "test", {"x": 1})
    assert UIActionService(db).consume(user, token, "test").payload == {"x": 1}
    with pytest.raises(AppError):
        UIActionService(db).consume(user, token)
    with pytest.raises(AppError):
        UIActionService(db).consume(user, "bad")
    box = SecretBox("key")
    assert box.decrypt(box.encrypt("secret")) == "secret"
    with pytest.raises(AppError):
        box.decrypt("invalid")


def test_oauth_flow_and_replay(db, monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gmail-client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "gmail-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "calendar-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "calendar-secret")
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        OAuthService,
        "_exchange",
        lambda self, provider, code, uri: {
            "access_token": "real-access",
            "refresh_token": "real-refresh",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        OAuthService, "_account_id", lambda self, provider, token: "mail@example.com"
    )
    user = UserService(db).get_or_create(4)
    service = OAuthService(db)
    state = service.create_state(user, "gmail")
    assert "accounts.google.com" in service.authorize_url("gmail", state.state_token)
    source = service.complete("gmail", state.state_token, "code")
    assert source.status == "active"
    from app.models.entities import SourceCredential

    credential = db.query(SourceCredential).filter_by(source_id=source.id).one()
    assert SecretBox().decrypt(credential.encrypted_access_token) == "real-access"
    with pytest.raises(AppError):
        service.complete("gmail", state.state_token, "again")
    google = service.create_state(user, "google")
    connection = service.complete("google", google.state_token, "code")
    assert connection.status == "active"
    with pytest.raises(AppError):
        service.create_state(user, "unknown")
    get_settings.cache_clear()


def test_oauth_http(client, headers, monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gmail-client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "gmail-secret")
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        OAuthService, "_exchange", lambda self, provider, code, uri: {"access_token": "access"}
    )
    monkeypatch.setattr(OAuthService, "_account_id", lambda self, provider, token: "x@y")
    created = client.post("/api/v1/oauth/gmail/states", headers=headers).json()["data"]
    response = client.get(created["authorize_url"], follow_redirects=False)
    assert response.status_code == 302
    callback = client.get(
        "/api/v1/oauth/gmail/callback",
        params={"state": created["state"], "code": "abc", "external_account_id": "attacker@y"},
    )
    assert callback.status_code == 200 and "Подключено" in callback.text
    assert "text/html" in callback.headers["content-type"]
    assert (
        client.get(
            "/api/v1/oauth/gmail/callback", params={"state": created["state"], "code": "abc"}
        ).status_code
        == 401
    )
    get_settings.cache_clear()


def test_webhook_idempotency_and_callback(client, db, monkeypatch):
    from app.workers.tasks import process_message

    monkeypatch.setattr(process_message, "delay", Mock())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}
    payload = {
        "update_id": 10,
        "message": {
            "message_id": 20,
            "from": {"id": 777, "first_name": "A"},
            "chat": {"id": 777, "type": "private"},
            "text": "Создай задачу тест",
        },
    }
    first = client.post("/webhooks/telegram", headers=headers, json=payload)
    assert first.status_code == 200 and "job_id" in first.json()
    assert client.post("/webhooks/telegram", headers=headers, json=payload).json()["duplicate"]
    assert client.post("/webhooks/telegram", json=payload).status_code == 401
    assert client.post("/webhooks/telegram", headers=headers, json={"x": 1}).status_code == 422
    ignored = client.post(
        "/webhooks/telegram", headers=headers, json={"update_id": 11, "edited_message": {}}
    )
    assert ignored.json()["ignored"]
    user = UserService(db).get_or_create(777)
    result = AIResult(
        intent="general_query",
        confidence=0.9,
        entities={},
        action=AIAction(type="none", requires_confirmation=False),
        reason="ok",
    )
    token = UIActionService(db).create(
        user, "confirm_ai", {"result": result.model_dump(mode="json"), "message_id": None}
    )
    callback = {"update_id": 12, "callback_query": {"from": {"id": 777}, "data": token}}
    assert (
        client.post("/webhooks/telegram", headers=headers, json=callback).json()["outcome"]["state"]
        == "informational"
    )


def test_webhook_rejects_unconnected_group_and_malformed_sender(client):
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}
    group = {
        "update_id": 501,
        "message": {
            "message_id": 9,
            "from": {"id": 123},
            "chat": {"id": -10001, "type": "supergroup", "title": "Unconnected"},
            "text": "Create a task",
        },
    }
    response = client.post("/webhooks/telegram", headers=headers, json=group)
    assert response.json()["reason"] == "source_not_connected"
    bad = {"update_id": 502, "message": {"message_id": 10, "chat": {"type": "private"}}}
    assert client.post("/webhooks/telegram", headers=headers, json=bad).status_code == 422


def test_group_connect_admin_and_member_messages_belong_to_owner(client, db, monkeypatch):
    from app.integrations.telegram.client import TelegramClient
    from app.workers.tasks import process_message

    monkeypatch.setattr(
        TelegramClient,
        "get_chat_member",
        lambda self, chat, user: {"status": "administrator" if user == 701 else "member"},
    )
    monkeypatch.setattr(TelegramClient, "send_message", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(process_message, "delay", Mock())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    def group(update, sender, text):
        return {
            "update_id": update,
            "message": {
                "message_id": update,
                "from": {"id": sender},
                "chat": {"id": -700, "type": "supergroup", "title": "Team"},
                "text": text,
            },
        }

    denied = client.post(
        "/webhooks/telegram", headers=headers, json=group(800, 702, "/connect")
    ).json()
    assert denied["connected"] is False
    connected = client.post(
        "/webhooks/telegram", headers=headers, json=group(801, 701, "/connect")
    ).json()
    assert connected["connected"] is True
    assert (
        client.post("/webhooks/telegram", headers=headers, json=group(802, 702, "/today")).json()[
            "reason"
        ]
        == "private_command"
    )
    stored = client.post(
        "/webhooks/telegram", headers=headers, json=group(803, 702, "Create a task")
    ).json()
    assert "job_id" in stored
    owner = UserService(db).get_or_create(701)
    message = db.query(Message).filter_by(external_message_id="803").one()
    assert message.user_id == owner.id and message.sender_external_id == "702"


def test_email_callback_failure_keeps_draft_visible(client, db, monkeypatch):
    from app.integrations.telegram.client import TelegramClient
    from app.services.email import EmailService

    sent_messages = Mock(return_value={"message_id": 1})
    monkeypatch.setattr(TelegramClient, "send_message", sent_messages)
    monkeypatch.setattr(TelegramClient, "answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(
        EmailService,
        "send_consumed_reply",
        Mock(side_effect=AppError("MAIL_PROVIDER_ERROR", "SMTP failed", 502)),
    )
    user = UserService(db).get_or_create(9991)
    token = UIActionService(db).create(
        user,
        "send_email",
        {"message_id": "11111111-1111-1111-1111-111111111111", "draft_text": "Draft body"},
    )
    payload = {
        "update_id": 9001,
        "callback_query": {
            "id": "callback",
            "from": {"id": 9991},
            "data": token,
            "message": {"chat": {"id": 9991, "type": "private"}, "message_id": 99},
        },
    }
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}
    result = client.post("/webhooks/telegram", headers=headers, json=payload)
    assert result.status_code == 200 and result.json()["sent"] is False
    assert "Draft body" in sent_messages.call_args.args[1]
    replay = client.post("/webhooks/telegram", headers=headers, json={**payload, "update_id": 9002})
    assert replay.json()["expired"] is True


def test_bot_email_connect_menu_and_authorize_link(client, db, monkeypatch):
    from app.core.config import get_settings
    from app.integrations.telegram.client import TelegramClient

    monkeypatch.setenv("GMAIL_CLIENT_ID", "my-app")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "my-secret")
    get_settings.cache_clear()
    send = Mock(return_value={"message_id": 1})
    monkeypatch.setattr(TelegramClient, "send_message", send)
    monkeypatch.setattr(TelegramClient, "answer_callback", lambda *a, **k: None)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}
    menu = client.post(
        "/webhooks/telegram",
        headers=headers,
        json={
            "update_id": 9101,
            "message": {
                "message_id": 1,
                "from": {"id": 9101},
                "chat": {"id": 9101, "type": "private"},
                "text": "/connect",
            },
        },
    )
    assert menu.json()["command"] == "connect"
    buttons = send.call_args.args[2]["inline_keyboard"]
    assert {button["text"] for row in buttons for button in row} == {
        "Gmail",
        "Яндекс.Почта",
        "Mail.ru",
        "Другой IMAP",
        "Google Calendar",
    }
    gmail_token = buttons[0][0]["callback_data"]
    callback = client.post(
        "/webhooks/telegram",
        headers=headers,
        json={
            "update_id": 9102,
            "callback_query": {
                "id": "callback",
                "from": {"id": 9101},
                "data": gmail_token,
                "message": {"chat": {"id": 9101, "type": "private"}},
            },
        },
    )
    assert callback.json()["provider"] == "gmail"
    assert "accounts.google.com" in send.call_args.args[2]["inline_keyboard"][0][0]["url"]
    get_settings.cache_clear()


def test_storage_adapter():
    client = Mock()
    client.list_buckets.return_value = {"Buckets": []}
    client.generate_presigned_url.return_value = "signed"
    storage = S3Storage(client)
    storage.ensure_bucket()
    client.create_bucket.assert_called_once()
    key, checksum = storage.put(
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        "a.txt",
        b"hello",
        "text/plain",
    )
    assert key.endswith(".txt") and len(checksum) == 64
    assert storage.signed_url(key) == "signed"
