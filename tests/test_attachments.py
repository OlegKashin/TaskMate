from datetime import UTC, datetime

from sqlalchemy import select

from app.models.entities import Attachment, Message, Source
from app.services.attachments import save_telegram_attachment
from app.services.domain import UserService


def test_telegram_document_saved_to_local_storage_once(db, monkeypatch):
    user = UserService(db).get_or_create(7001)
    source = Source(user_id=user.id, type="telegram_chat", name="Chat", status="active", external_source_id="7001")
    db.add(source)
    db.flush()
    message = Message(
        user_id=user.id, source_id=source.id, external_message_id="doc-1", message_type="document",
        received_at=datetime.now(UTC), raw_payload={"message": {"document": {
            "file_id": "abc", "file_name": "report.pdf", "mime_type": "application/pdf"
        }}},
    )
    db.add(message)
    db.commit()
    from app.integrations.telegram.client import TelegramClient

    monkeypatch.setattr(TelegramClient, "download_file", lambda self, file_id, limit: b"file")

    class Storage:
        calls = 0

        def put(self, user_id, message_id, attachment_id, filename, content, mime):
            self.__class__.calls += 1
            assert filename == "report.pdf" and content == b"file"
            return "users/key.pdf", "checksum"

    monkeypatch.setattr("app.services.attachments.S3Storage", Storage)
    attachment = save_telegram_attachment(db, message)
    assert attachment.storage_key == "users/key.pdf" and attachment.checksum == "checksum"
    assert attachment.size_bytes == 4
    assert save_telegram_attachment(db, message) is None
    assert Storage.calls == 1
    assert db.scalar(select(Attachment).where(Attachment.message_id == message.id))


def test_telegram_photo_respects_source_setting(db, monkeypatch):
    user = UserService(db).get_or_create(7002)
    source = Source(user_id=user.id, type="telegram_chat", name="Chat", status="active", external_source_id="7002", save_attachments=False)
    db.add(source)
    db.flush()
    message = Message(
        user_id=user.id, source_id=source.id, external_message_id="photo-1", message_type="photo",
        raw_payload={"message": {"photo": [{"file_id": "small"}, {"file_id": "large"}]}},
    )
    db.add(message)
    db.commit()
    assert save_telegram_attachment(db, message) is None
    source.save_attachments = True
    db.commit()
    from app.integrations.telegram.client import TelegramClient

    monkeypatch.setattr(TelegramClient, "download_file", lambda self, file_id, limit: file_id.encode())

    class Storage:
        def put(self, user_id, message_id, attachment_id, filename, content, mime):
            assert filename == "photo.jpg" and content == b"large"
            return "photo-key", "checksum"

    monkeypatch.setattr("app.services.attachments.S3Storage", Storage)
    attachment = save_telegram_attachment(db, message)
    assert attachment.mime_type == "image/jpeg"
