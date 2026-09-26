import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import or_, select

from app.ai.service import AIService
from app.bot.presentation import outcome_message
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.integrations.telegram.client import TelegramClient
from app.models.entities import (
    AIProcessingJob,
    Message,
    Notification,
    OAuthState,
    Source,
    UIAction,
    User,
    utcnow,
)
from app.services.attachments import save_telegram_attachment
from app.services.domain import (
    CalendarService,
    NotificationService,
    ReminderService,
    UIActionService,
)
from app.services.email import EmailService
from app.workers.celery_app import celery_app

RETRY_DELAYS = (30, 120, 600)
RETRYABLE_CODES = {"LLM_REQUEST_FAILED", "STT_REQUEST_FAILED", "TELEGRAM_FILE_ERROR"}


@celery_app.task(bind=True, max_retries=3)
def process_message(self, job_id: str):
    with SessionLocal() as db:
        job = db.get(AIProcessingJob, uuid.UUID(job_id))
        if not job or job.status == "completed":
            return None
        message = db.get(Message, job.message_id)
        user = db.get(User, job.user_id)
        if not message or not user:
            return None
        chat_id = (message.raw_payload or {}).get("message", {}).get("chat", {}).get("id")
        if chat_id is None:
            chat_id = (message.raw_payload or {}).get("channel_post", {}).get("chat", {}).get("id")
        try:
            save_telegram_attachment(db, message)
            if job.job_type == "attachment_only" or (
                message.message_type in {"photo", "document"} and not message.text
            ):
                job.status = "completed"
                message.processing_status = "processed"
                db.commit()
                outcome = {"state": "informational", "reason": "Вложение сохранено."}
            else:
                outcome = asyncio.run(AIService(db).process(job, message, user))
        except Exception as exc:
            retryable = isinstance(exc, TimeoutError) or (
                isinstance(exc, AppError) and exc.code in RETRYABLE_CODES
            )
            will_retry = retryable and self.request.retries < len(RETRY_DELAYS)
            job.status = "queued" if will_retry else "failed"
            job.error_code = exc.code if isinstance(exc, AppError) else "PROCESSING_ERROR"
            message.processing_status = "queued" if will_retry else "failed"
            db.commit()
            if will_retry:
                raise self.retry(exc=exc, countdown=RETRY_DELAYS[self.request.retries]) from exc
            if chat_id is not None:
                try:
                    client = TelegramClient()
                    failure = (
                        "Распознавание голосовых пока не настроено. Отправьте текстом."
                        if job.error_code == "STT_NOT_CONFIGURED"
                        else "Сейчас не получилось обработать сообщение. Оно сохранено, можно повторить позже."
                    )
                    markup = None
                    if job.error_code != "STT_NOT_CONFIGURED":
                        retry_token = UIActionService(db).create(
                            user, "retry_job", {"id": str(job.id)}, ttl_seconds=86400,
                        )
                        later_token = UIActionService(db).create(
                            user, "leave_job", {"id": str(job.id)}, ttl_seconds=86400,
                        )
                        markup = {"inline_keyboard": [[
                            {"text": "Повторить", "callback_data": retry_token},
                            {"text": "Оставить на потом", "callback_data": later_token},
                        ]]}
                    if job.status_message_id:
                        client.edit_message(chat_id, job.status_message_id, failure, markup)
                    else:
                        client.send_message(chat_id, failure, markup)
                except Exception:
                    pass
            else:
                db.add(Notification(
                    user_id=user.id, type="ai_processing_failed",
                    payload={"title": "Не удалось обработать письмо. Оно сохранено; запустите /analyze для повтора."},
                    dedupe_key=f"ai-failed:{job.id}",
                ))
                db.commit()
            raise
        if chat_id is not None:
            text, markup = outcome_message(db, user, outcome)
            client = TelegramClient()
            if job.status_message_id:
                client.edit_message(chat_id, job.status_message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
        return outcome


@celery_app.task(bind=True, max_retries=3)
def sync_mail_source(self, source_id: str):
    from app.services.analysis import queue_source_analysis

    with SessionLocal() as db:
        source = db.get(Source, uuid.UUID(source_id))
        if not source or source.status != "active":
            return {"imported": 0, "status": "inactive"}
        user = db.get(User, source.user_id)
        try:
            imported = EmailService(db).sync(user, source.id)
            analysis = queue_source_analysis(db, user, source.id, sync_external=False)
            return {"imported": imported, "status": "completed", "analysis": analysis}
        except Exception as exc:
            retryable = isinstance(exc, TimeoutError) or (
                isinstance(exc, AppError) and exc.code == "MAIL_PROVIDER_ERROR"
            )
            if retryable and self.request.retries < len(RETRY_DELAYS):
                db.rollback()
                raise self.retry(exc=exc, countdown=RETRY_DELAYS[self.request.retries]) from exc
            if retryable or (
                isinstance(exc, AppError) and exc.code == "MAIL_NOT_CONFIGURED"
            ):
                db.rollback()
                source = db.get(Source, uuid.UUID(source_id))
                if source.status == "active":
                    source.status = "error"
                    db.add(Notification(
                        user_id=source.user_id, type="source_error",
                        payload={"title": f"Не удалось синхронизировать почту «{source.name}». Проверьте подключение."},
                    ))
                    db.commit()
            raise


@celery_app.task
def schedule_tick():
    with SessionLocal() as db:
        fired = ReminderService(db).fire_due()
        notifications = NotificationService(db)
        briefings = notifications.enqueue_briefings()
        sent = notifications.deliver_pending()
        events = CalendarService(db).sync_pending()
        source_ids = db.scalars(select(Source.id).where(
            Source.status == "active",
            Source.type.in_(["gmail", "yandex", "mailru", "imap"]),
        ).order_by(Source.last_synced_at.asc()).limit(25)).all()
        queued = 0
        for source_id in source_ids:
            try:
                sync_mail_source.delay(str(source_id))
                queued += 1
            except Exception:
                pass
        mail = {"sources": len(source_ids), "queued": queued}
        return {"reminders_fired": fired, "briefings_created": briefings, "notifications_sent": sent, "calendar_events_synced": events, "mail": mail}


@celery_app.task
def cleanup_expired():
    settings, now = get_settings(), utcnow()
    consumed_before = now - timedelta(hours=settings.ui_actions_cleanup_retention_hours)
    with SessionLocal() as db:
        actions = (
            db.query(UIAction)
            .filter(or_(UIAction.expires_at < now, UIAction.consumed_at < consumed_before))
            .delete(synchronize_session=False)
        )
        states = (
            db.query(OAuthState)
            .filter(or_(OAuthState.expires_at < now, OAuthState.consumed_at < consumed_before))
            .delete(synchronize_session=False)
        )
        db.commit()
        return {"ui_actions": actions, "oauth_states": states}
