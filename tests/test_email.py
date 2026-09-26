import base64
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import SecretBox
from app.integrations.email import (
    MailAttachment,
    MailRecord,
    gmail_messages,
    imap_messages,
    parse_mail,
    send_reply,
)
from app.models.entities import (
    Attachment,
    InboxItem,
    Message,
    Source,
    SourceCredential,
    SourceFolder,
)
from app.services.analysis import queue_source_analysis
from app.services.domain import InboxService, UserService
from app.services.email import EmailService
from app.services.oauth import OAuthService
from app.workers.tasks import sync_mail_source


def raw_mail(sender="Alice <alice@example.com>", message_id="<m1@example.com>"):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "owner@example.com"
    mail["Subject"] = "Status"
    mail["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if message_id:
        mail["Message-ID"] = message_id
    mail.set_content("Please reply")
    return mail.as_bytes()


def response(data):
    item = Mock()
    item.json.return_value = data
    item.raise_for_status.return_value = None
    return item


def mail_source(db, user, provider="gmail"):
    start = datetime.now(UTC) - timedelta(minutes=10)
    source = Source(
        user_id=user.id,
        type=provider,
        name="Inbox",
        status="active",
        external_source_id="owner@example.com",
        connected_at=start,
        last_synced_at=start,
        last_analyzed_at=start,
    )
    db.add(source)
    db.flush()
    credential = SourceCredential(
        source_id=source.id,
        provider=provider,
        encrypted_username=SecretBox().encrypt("owner@example.com"),
        encrypted_access_token=SecretBox().encrypt("access"),
    )
    db.add(credential)
    db.commit()
    return source, credential


def test_mail_parser_header_and_uid_fallback():
    record = parse_mail(raw_mail(), "uid")
    assert record.external_id == "<m1@example.com>"
    assert record.sender == "alice@example.com" and record.sender_name == "Alice"
    assert record.text.strip() == "Please reply"
    fallback = parse_mail(raw_mail(message_id=""), "INBOX:123:44")
    assert fallback.external_id == "INBOX:123:44"


def test_mail_parser_collects_attachment():
    mail = EmailMessage()
    mail["From"] = "alice@example.com"
    mail["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    mail.set_content("Body")
    mail.add_attachment(b"report", maintype="text", subtype="plain", filename="report.txt")
    parsed = parse_mail(mail.as_bytes(), "uid")
    assert parsed.text.strip() == "Body"
    assert parsed.attachments[0].filename == "report.txt"
    assert parsed.attachments[0].content == b"report"


def test_gmail_pagination_and_internal_date(monkeypatch):
    import app.integrations.email as adapter

    raw = base64.urlsafe_b64encode(raw_mail()).decode().rstrip("=")
    listing = [
        response({"messages": [{"id": "g1"}], "nextPageToken": "next"}),
        response({"messages": [{"id": "g2"}]}),
    ]
    details = response(
        {
            "raw": raw,
            "threadId": "thread",
            "internalDate": str(int(datetime.now(UTC).timestamp() * 1000)),
        }
    )
    seen = []

    def get(url, **kwargs):
        seen.append((url, kwargs))
        return listing.pop(0) if url.endswith("/messages") else details

    monkeypatch.setattr(adapter.httpx, "get", get)
    records = gmail_messages("token", datetime.now(UTC) - timedelta(days=1), ["INBOX"])
    assert len(records) == 2 and records[0].thread_id == "thread"
    assert seen[0][1]["params"]["labelIds"] == ["INBOX"]
    assert seen[2][1]["params"]["pageToken"] == "next"


def test_gmail_rejects_failed_request_and_oversize(monkeypatch):
    import app.integrations.email as adapter

    bad = Mock()
    bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad",
        request=httpx.Request("GET", "https://gmail.googleapis.com"),
        response=httpx.Response(500),
    )
    monkeypatch.setattr(adapter.httpx, "get", lambda *a, **k: bad)
    with pytest.raises(AppError, match="Mail provider request failed"):
        gmail_messages("token", datetime.now(UTC), [])
    monkeypatch.setattr(
        adapter.httpx, "get", lambda *a, **k: response({"messages": [{"id": "a"}, {"id": "b"}]})
    )
    with pytest.raises(AppError) as exc:
        gmail_messages("token", datetime.now(UTC), [], limit=1)
    assert exc.value.code == "MAIL_BATCH_TOO_LARGE"


def test_imap_oauth_fetch_uid_fallback_and_limit(monkeypatch):
    import app.integrations.email as adapter

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.select.return_value = ("OK", [b"1"])
    client.response.return_value = ("UIDVALIDITY", [b"42"])
    client.uid.side_effect = [("OK", [b"7"]), ("OK", [(b"7 (RFC822)", raw_mail(message_id=""))])]
    monkeypatch.setattr(adapter.imaplib, "IMAP4_SSL", Mock(return_value=client))
    records = imap_messages(
        "yandex", "owner@example.com", "token", datetime.now(UTC) - timedelta(days=1), ["INBOX"]
    )
    assert records[0].external_id == "42:7"
    assert client.authenticate.call_args.args[0] == "XOAUTH2"
    assert b"Bearer token" in client.authenticate.call_args.args[1](None)
    client.uid.side_effect = None
    client.uid.return_value = ("OK", [b"7 8"])
    with pytest.raises(AppError) as exc:
        imap_messages(
            "yandex",
            "owner@example.com",
            "token",
            datetime.now(UTC) - timedelta(days=1),
            [],
            limit=1,
        )
    assert exc.value.code == "MAIL_BATCH_TOO_LARGE"


def test_send_reply_gmail_and_smtp(monkeypatch):
    import app.integrations.email as adapter

    posted = Mock(return_value=response({"id": "sent-id"}))
    monkeypatch.setattr(adapter.httpx, "post", posted)
    assert (
        send_reply(
            "gmail",
            "owner@example.com",
            "token",
            "alice@example.com",
            "Status",
            "Thanks",
            "<m1@example.com>",
            "thread",
        )
        == "sent-id"
    )
    body = posted.call_args.kwargs["json"]
    sent = parse_mail(base64.urlsafe_b64decode(body["raw"] + "==="), "fallback")
    assert sent.subject == "Re: Status" and body["threadId"] == "thread"
    smtp = Mock()
    smtp.__enter__ = Mock(return_value=smtp)
    smtp.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(adapter.smtplib, "SMTP_SSL", Mock(return_value=smtp))
    send_reply(
        "mailru", "owner@example.com", "token", "alice@example.com", "Re: Status", "Thanks", None
    )
    smtp.auth.assert_called_once()
    smtp.send_message.assert_called_once()
    with pytest.raises(AppError) as exc:
        send_reply("gmail", "owner@example.com", "token", "", "Status", "x", None)
    assert exc.value.code == "INVALID_RECIPIENT"


def test_sync_imports_only_new_messages_and_deduplicates(db, monkeypatch):
    import app.services.email as service_module

    user = UserService(db).get_or_create(4001)
    source, _ = mail_source(db, user)
    db.add(SourceFolder(source_id=source.id, external_folder_id="INBOX", name="Inbox"))
    db.commit()
    record = MailRecord(
        "<m1@example.com>",
        "thread",
        "alice@example.com",
        "Alice",
        "Status",
        "Please reply",
        datetime.now(UTC),
        "<m1@example.com>",
    )
    captured = []

    def fetch(token, since, labels):
        captured.append((token, since, labels))
        return [record]

    monkeypatch.setattr(service_module, "gmail_messages", fetch)
    assert EmailService(db).sync(user, source.id) == 1
    assert captured[0][2] == ["INBOX"]
    assert EmailService(db).sync(user, source.id) == 0
    messages = db.scalars(select(Message).where(Message.source_id == source.id)).all()
    assert len(messages) == 1 and messages[0].sender_email == "alice@example.com"
    assert source.last_synced_at > source.connected_at


def test_sync_error_does_not_advance_checkpoint(db, monkeypatch):
    import app.services.email as service_module

    user = UserService(db).get_or_create(4002)
    source, _ = mail_source(db, user)
    before = source.last_synced_at
    monkeypatch.setattr(
        service_module,
        "gmail_messages",
        Mock(side_effect=AppError("MAIL_PROVIDER_ERROR", "offline", 502)),
    )
    with pytest.raises(AppError):
        EmailService(db).sync(user, source.id)
    assert source.last_synced_at == before
    assert EmailService(db).sync_all()["failed"] == 1


def test_sync_stores_attachment_in_local_s3(db, monkeypatch):
    import app.services.email as service_module

    user = UserService(db).get_or_create(4004)
    source, _ = mail_source(db, user)
    record = MailRecord(
        "<attachment@example.com>",
        None,
        "alice@example.com",
        "Alice",
        "Report",
        "Attached",
        datetime.now(UTC),
        "<attachment@example.com>",
        [MailAttachment("report.txt", "text/plain", b"report")],
    )
    monkeypatch.setattr(service_module, "gmail_messages", Mock(return_value=[record]))
    storage = Mock()
    storage.put.return_value = ("users/local/report.txt", "checksum")
    monkeypatch.setattr(service_module, "S3Storage", Mock(return_value=storage))
    assert EmailService(db).sync(user, source.id) == 1
    attachment = db.scalar(select(Attachment))
    assert attachment.storage_key == "users/local/report.txt"
    assert attachment.size_bytes == 6 and attachment.checksum == "checksum"
    storage.put.assert_called_once()


def test_reply_requires_confirmation_and_cannot_replay(db, monkeypatch):
    import app.services.email as service_module

    user = UserService(db).get_or_create(4003)
    source, _ = mail_source(db, user)
    message = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="<m2@example.com>",
        message_type="email",
        sender_email="alice@example.com",
        subject="Question",
        text="Hi",
        raw_payload={"message_id_header": "<m2@example.com>"},
        received_at=datetime.now(UTC),
    )
    db.add(message)
    db.flush()
    item = InboxItem(user_id=user.id, message_id=message.id, item_type="reply_required")
    db.add(item)
    db.commit()
    sent = Mock(return_value="external-sent")
    monkeypatch.setattr(service_module, "send_reply", sent)
    token = InboxService(db).reply(user, item.id, "Thanks")
    sent.assert_not_called()
    assert EmailService(db).confirm_reply(user, token) == "external-sent"
    assert sent.call_args.args[3:6] == ("alice@example.com", "Question", "Thanks")
    with pytest.raises(AppError):
        EmailService(db).confirm_reply(user, token)
    assert sent.call_count == 1
    with pytest.raises(AppError):
        EmailService(db).draft_for_message(user, uuid.uuid4(), "x")


def test_imap_configuration_and_analysis(client, db, headers, monkeypatch):
    import app.services.email as service_module

    user = UserService(db).get_or_create(1001)
    source = Source(user_id=user.id, type="imap", name="Custom", status="connecting")
    db.add(source)
    db.commit()
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    get_settings.cache_clear()
    result = client.post(
        f"/api/v1/sources/{source.id}/imap-credentials",
        headers=headers,
        json={"username": "owner@example.com", "password": "app-password"},
    )
    assert result.status_code == 200
    credential = db.scalar(select(SourceCredential).where(SourceCredential.source_id == source.id))
    assert SecretBox().decrypt(credential.encrypted_password) == "app-password"
    monkeypatch.setattr(service_module, "imap_messages", Mock(return_value=[]))
    monkeypatch.setattr(sync_mail_source, "delay", Mock())
    assert queue_source_analysis(db, user, source.id)["external_sync"] == "queued"
    get_settings.cache_clear()


def test_mailru_oauth_verifies_claimed_mailbox(db, monkeypatch):
    import app.services.oauth as oauth_module

    monkeypatch.setenv("MAILRU_CLIENT_ID", "my-app")
    monkeypatch.setenv("MAILRU_CLIENT_SECRET", "my-secret")
    get_settings.cache_clear()
    user = UserService(db).get_or_create(4010)
    service = OAuthService(db)
    with pytest.raises(AppError):
        service.create_state(user, "mailru")
    state = service.create_state(user, "mailru", "owner@mail.ru")
    assert state.purpose == "source" and state.source_id
    assert "scope=mail.imap" in service.authorize_url("mailru", state.state_token)
    monkeypatch.setattr(
        OAuthService,
        "_exchange",
        lambda *a: {
            "access_token": "provider-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        },
    )
    imap = Mock()
    imap.__enter__ = Mock(return_value=imap)
    imap.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(oauth_module.imaplib, "IMAP4_SSL", Mock(return_value=imap))
    source = service.complete("mailru", state.state_token, "code")
    assert source.status == "active" and source.external_source_id == "owner@mail.ru"
    assert (
        db.scalar(
            select(SourceFolder).where(SourceFolder.source_id == source.id)
        ).external_folder_id
        == "INBOX"
    )
    assert b"owner@mail.ru" in imap.authenticate.call_args.args[1](None)
    credential = db.scalar(select(SourceCredential).where(SourceCredential.source_id == source.id))
    assert SecretBox().decrypt(credential.encrypted_access_token) == "provider-token"
    with pytest.raises(AppError):
        service.complete("mailru", state.state_token, "code")
    get_settings.cache_clear()


def test_yandex_profile_uses_oauth_header(db, monkeypatch):
    import app.services.oauth as oauth_module

    get = Mock(return_value=response({"default_email": "owner@yandex.ru", "id": "42"}))
    monkeypatch.setattr(oauth_module.httpx, "get", get)
    assert OAuthService(db)._account_id("yandex", "token") == "owner@yandex.ru"
    assert get.call_args.kwargs["headers"] == {"Authorization": "OAuth token"}


def test_oauth_denial_marks_connecting_source_error(client, db, headers, monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "my-app")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "my-secret")
    get_settings.cache_clear()
    created = client.post("/api/v1/oauth/gmail/states", headers=headers).json()["data"]
    result = client.get(
        "/api/v1/oauth/gmail/callback", params={"state": created["state"], "error": "access_denied"}
    )
    assert result.status_code == 400 and "Подключение отменено" in result.text
    source = db.scalar(select(Source).where(Source.type == "gmail"))
    assert source.status == "error"
    assert (
        client.get(
            "/api/v1/oauth/gmail/callback", params={"state": created["state"], "code": "late"}
        ).status_code
        == 401
    )
    get_settings.cache_clear()


def test_expired_gmail_token_refreshes_before_sync(db, monkeypatch):
    import app.services.email as service_module
    import app.services.oauth as oauth_module

    monkeypatch.setenv("GMAIL_CLIENT_ID", "my-app")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "my-secret")
    get_settings.cache_clear()
    user = UserService(db).get_or_create(4011)
    source, credential = mail_source(db, user)
    credential.encrypted_refresh_token = SecretBox().encrypt("old-refresh")
    credential.token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    post = Mock(return_value=response({"access_token": "new-token", "expires_in": 3600}))
    monkeypatch.setattr(oauth_module.httpx, "post", post)
    fetch = Mock(return_value=[])
    monkeypatch.setattr(service_module, "gmail_messages", fetch)
    assert EmailService(db).sync(user, source.id) == 0
    assert fetch.call_args.args[0] == "new-token"
    assert SecretBox().decrypt(credential.encrypted_access_token) == "new-token"
    get_settings.cache_clear()


def test_worker_imports_then_queues_ai_job(db, monkeypatch):
    import app.services.email as email_module
    import app.workers.tasks as task_module

    user = UserService(db).get_or_create(4012)
    source, _ = mail_source(db, user)
    record = MailRecord(
        "<worker@example.com>",
        "thread",
        "alice@example.com",
        "Alice",
        "Action",
        "Create a task",
        datetime.now(UTC),
        "<worker@example.com>",
    )
    monkeypatch.setattr(email_module, "gmail_messages", Mock(return_value=[record]))
    monkeypatch.setattr(task_module, "SessionLocal", lambda: nullcontext(db))
    queued = Mock()
    monkeypatch.setattr(task_module.process_message, "delay", queued)
    result = sync_mail_source.run(str(source.id))
    assert result["imported"] == 1 and result["analysis"]["queued"] == 1
    queued.assert_called_once()
    message = db.scalar(
        select(Message).where(Message.external_message_id == "<worker@example.com>")
    )
    assert message.processing_status == "queued"


def test_reply_api_draft_confirm_and_replay(client, db, headers, monkeypatch):
    import app.services.email as service_module

    user = UserService(db).get_or_create(1001)
    source, _ = mail_source(db, user)
    message = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="api-email",
        message_type="email",
        sender_email="alice@example.com",
        subject="Question",
        text="Hi",
        received_at=datetime.now(UTC),
    )
    db.add(message)
    db.flush()
    item = InboxItem(user_id=user.id, message_id=message.id, item_type="reply_required")
    db.add(item)
    db.commit()
    send = Mock(return_value="sent")
    monkeypatch.setattr(service_module, "send_reply", send)
    draft = client.post(
        f"/api/v1/inbox/{item.id}/reply", headers=headers, json={"draft_text": "Thanks"}
    )
    assert draft.status_code == 200 and send.call_count == 0
    token = draft.json()["data"]["confirmation_token"]
    confirmed = client.post("/api/v1/inbox/replies/confirm", headers=headers, json={"token": token})
    assert confirmed.json()["data"]["external_message_id"] == "sent"
    assert send.call_count == 1
    assert (
        client.post(
            "/api/v1/inbox/replies/confirm", headers=headers, json={"token": token}
        ).status_code
        == 409
    )


def test_mail_sources_cannot_be_activated_without_credentials(client, headers):
    denied = client.post(
        "/api/v1/sources",
        headers=headers,
        json={"type": "gmail", "name": "Gmail", "external_source_id": "owner@example.com"},
    )
    assert denied.status_code == 422
    imap = client.post(
        "/api/v1/sources",
        headers=headers,
        json={"type": "imap", "name": "Custom", "external_source_id": "owner@example.com"},
    )
    assert imap.status_code == 201 and imap.json()["data"]["status"] == "connecting"
    source_id = imap.json()["data"]["id"]
    assert (
        client.patch(
            f"/api/v1/sources/{source_id}", headers=headers, json={"status": "active"}
        ).status_code
        == 422
    )
