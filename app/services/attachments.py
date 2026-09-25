"""Persist Telegram attachments in the local SeaweedFS S3 bucket."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.storage import S3Storage
from app.integrations.telegram.client import TelegramClient
from app.models.entities import Attachment, Message, Source


def save_telegram_attachment(db: Session, message: Message) -> Attachment | None:
    if message.message_type not in {"photo", "document", "voice"}:
        return None
    if db.scalar(select(Attachment).where(Attachment.message_id == message.id)):
        return None
    source = db.get(Source, message.source_id)
    if not source or not source.save_attachments:
        return None
    raw = (message.raw_payload or {}).get("message") or (message.raw_payload or {}).get("channel_post") or {}
    if message.message_type == "photo":
        photos = raw.get("photo") or []
        info = photos[-1] if photos else {}
        filename, mime = "photo.jpg", "image/jpeg"
    elif message.message_type == "voice":
        info = raw.get("voice") or {}
        filename, mime = "voice.ogg", "audio/ogg"
    else:
        info = raw.get("document") or {}
        filename = info.get("file_name") or "document.bin"
        mime = info.get("mime_type") or "application/octet-stream"
    if not info.get("file_id"):
        return None
    content = TelegramClient().download_file(info["file_id"], get_settings().max_voice_size_bytes)
    attachment = Attachment(
        id=uuid.uuid4(), message_id=message.id, filename=filename,
        mime_type=mime, size_bytes=len(content), storage_key="",
    )
    storage = S3Storage()
    key, checksum = storage.put(
        message.user_id, message.id, attachment.id, filename, content, mime
    )
    attachment.storage_key, attachment.checksum = key, checksum
    db.add(attachment)
    db.commit()
    return attachment
