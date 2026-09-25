"""Incremental mail import and explicitly confirmed replies."""

import uuid
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, conflict
from app.core.security import SecretBox
from app.integrations.email import gmail_messages, imap_messages, send_reply
from app.integrations.storage import S3Storage
from app.models.entities import (
    Attachment,
    Message,
    Source,
    SourceCredential,
    SourceFolder,
    UIAction,
    User,
    utcnow,
)
from app.services.domain import UIActionService


class EmailService:
    def __init__(self, db: Session):
        self.db = db

    def _source(self, user: User, source_id: uuid.UUID) -> Source:
        source = self.db.get(Source, source_id)
        if not source or source.user_id != user.id:
            raise AppError("SOURCE_NOT_FOUND", "Source not found", 404)
        if source.type not in {"gmail", "yandex", "mailru", "imap"}:
            raise conflict("MAIL_NOT_SUPPORTED_FOR_SOURCE", "Source is not a mailbox")
        if source.status != "active":
            raise conflict("SOURCE_INACTIVE", "Mailbox is not active")
        return source

    def _credentials(self, source: Source) -> tuple[str, str, bool]:
        credential = self.db.scalar(
            select(SourceCredential).where(SourceCredential.source_id == source.id)
        )
        if not credential:
            raise AppError("MAIL_NOT_CONFIGURED", "Mailbox credentials are missing", 503)
        box = SecretBox()
        username = (
            box.decrypt(credential.encrypted_username)
            if credential.encrypted_username
            else source.external_source_id
        )
        oauth = source.type != "imap"
        secret_value = credential.encrypted_access_token if oauth else credential.encrypted_password
        if not username or not secret_value:
            raise AppError("MAIL_NOT_CONFIGURED", "Mailbox credentials are missing", 503)
        expiry = credential.token_expires_at
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if oauth and expiry and expiry <= utcnow():
            from app.services.oauth import OAuthService

            OAuthService(self.db).refresh_source(source, credential)
            secret_value = credential.encrypted_access_token
        return username, box.decrypt(secret_value), oauth

    def configure_imap(
        self, user: User, source_id: uuid.UUID, username: str, password: str
    ) -> dict:
        source = self.db.get(Source, source_id)
        if not source or source.user_id != user.id:
            raise AppError("SOURCE_NOT_FOUND", "Source not found", 404)
        if source.type != "imap":
            raise conflict("MAIL_NOT_SUPPORTED_FOR_SOURCE", "Source is not an IMAP mailbox")
        settings = get_settings()
        if not settings.imap_host or not settings.smtp_host:
            raise AppError("MAIL_NOT_CONFIGURED", "IMAP_HOST and SMTP_HOST are required", 503)
        credential = self.db.scalar(
            select(SourceCredential).where(SourceCredential.source_id == source.id)
        )
        if not credential:
            credential = SourceCredential(source_id=source.id, provider="imap")
            self.db.add(credential)
        box = SecretBox()
        credential.encrypted_username = box.encrypt(username)
        credential.encrypted_password = box.encrypt(password)
        source.external_source_id = username
        source.status = "active"
        source.connected_at = source.last_synced_at = source.last_analyzed_at = utcnow()
        self.db.commit()
        return {"source_id": str(source.id), "status": source.status}

    def sync(self, user: User, source_id: uuid.UUID) -> int:
        source = self._source(user, source_id)
        username, secret, oauth = self._credentials(source)
        since = source.last_synced_at or source.connected_at
        if since is None:
            raise AppError("MAIL_NOT_CONFIGURED", "Mailbox has no connection time", 503)
        folders = list(
            self.db.scalars(
                select(SourceFolder).where(
                    SourceFolder.source_id == source.id, SourceFolder.is_selected.is_(True)
                )
            )
        )
        selected = [folder.external_folder_id for folder in folders]
        if source.type == "gmail":
            records = gmail_messages(secret, since, selected or ["INBOX"])
        else:
            settings = get_settings()
            records = imap_messages(
                source.type,
                username,
                secret,
                since,
                selected,
                oauth=oauth,
                host=settings.imap_host if source.type == "imap" else None,
            )
        count = 0
        for record in records:
            if self.db.scalar(
                select(Message.id).where(
                    Message.source_id == source.id,
                    Message.external_message_id == record.external_id,
                )
            ):
                continue
            message = Message(
                user_id=user.id,
                source_id=source.id,
                external_message_id=record.external_id,
                external_thread_id=record.thread_id,
                sender_email=record.sender,
                sender_name=record.sender_name,
                subject=record.subject,
                text=record.text,
                message_type="email",
                received_at=record.received_at,
                raw_payload={"message_id_header": record.message_id_header},
                processing_status="received" if source.analysis_text else "ignored",
            )
            self.db.add(message)
            self.db.flush()
            if source.save_attachments and record.attachments:
                storage = S3Storage()
                for file in record.attachments:
                    attachment = Attachment(
                        id=uuid.uuid4(),
                        message_id=message.id,
                        filename=file.filename,
                        mime_type=file.mime_type,
                        size_bytes=len(file.content),
                        storage_key="",
                    )
                    key, checksum = storage.put(
                        user.id,
                        message.id,
                        attachment.id,
                        file.filename,
                        file.content,
                        file.mime_type,
                    )
                    attachment.storage_key = key
                    attachment.checksum = checksum
                    self.db.add(attachment)
            count += 1
        source.last_synced_at = utcnow()
        self.db.commit()
        return count

    def confirm_reply(self, user: User, token: str) -> str:
        action = UIActionService(self.db).consume(user, token, "send_email")
        # Claim before external I/O: two callbacks can never send the same draft twice.
        self.db.commit()
        return self.send_consumed_reply(user, action)

    def send_consumed_reply(self, user: User, action: UIAction) -> str:
        message = self.db.get(Message, uuid.UUID(action.payload["message_id"]))
        if not message or message.user_id != user.id or message.message_type != "email":
            raise conflict("REPLY_NOT_SUPPORTED_FOR_SOURCE", "Email message unavailable")
        source = self._source(user, message.source_id)
        username, secret, oauth = self._credentials(source)
        settings = get_settings()
        return send_reply(
            source.type,
            username,
            secret,
            message.sender_email or "",
            message.subject or "",
            action.payload["draft_text"],
            (message.raw_payload or {}).get("message_id_header"),
            message.external_thread_id,
            oauth=oauth,
            host=settings.smtp_host if source.type == "imap" else None,
        )

    def draft_for_message(self, user: User, message_id: uuid.UUID, draft_text: str) -> str:
        message = self.db.get(Message, message_id)
        if not message or message.user_id != user.id or message.message_type != "email":
            raise conflict("REPLY_NOT_SUPPORTED_FOR_SOURCE", "Reply requires an email message")
        self._source(user, message.source_id)
        if not message.sender_email or not draft_text.strip():
            raise AppError("VALIDATION_ERROR", "Recipient and reply text are required", 422)
        return UIActionService(self.db).create(
            user,
            "send_email",
            {
                "message_id": str(message.id),
                "draft_text": draft_text.strip(),
            },
        )

    def sync_all(self, limit: int = 25) -> dict[str, int]:
        sources = self.db.scalars(
            select(Source)
            .where(
                Source.status == "active", Source.type.in_(["gmail", "yandex", "mailru", "imap"])
            )
            .limit(limit)
        ).all()
        result = {"sources": 0, "messages": 0, "failed": 0}
        for source in sources:
            user = self.db.get(User, source.user_id)
            try:
                result["messages"] += self.sync(user, source.id)
                result["sources"] += 1
                from app.services.analysis import queue_source_analysis

                queue_source_analysis(self.db, user, source.id, sync_external=False)
            except Exception:
                self.db.rollback()
                result["failed"] += 1
        return result
