import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import or_

from app.ai.service import AIService
from app.bot.presentation import outcome_message
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.integrations.telegram.client import TelegramClient
from app.models.entities import AIProcessingJob, Message, OAuthState, UIAction, User, utcnow
from app.services.attachments import save_telegram_attachment
from app.services.domain import CalendarService, NotificationService, ReminderService
from app.services.email import EmailService
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, max_retries=3)
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
        except Exception:
            job.status = "failed"
            job.error_code = job.error_code or "PROCESSING_ERROR"
            message.processing_status = "failed"
            db.commit()
            if chat_id is not None:
                try:
                    client = TelegramClient()
                    failure = (
                        "Распознавание голосовых пока не настроено. Отправьте текстом."
                        if job.error_code == "STT_NOT_CONFIGURED"
                        else "Не удалось обработать сообщение. Попробуйте отправить его ещё раз."
                    )
                    if job.status_message_id:
                        client.edit_message(chat_id, job.status_message_id, failure)
                    else:
                        client.send_message(chat_id, failure)
                except Exception:
                    pass
            raise
        if chat_id is not None:
            text, markup = outcome_message(db, user, outcome)
            client = TelegramClient()
            if job.status_message_id:
                client.edit_message(chat_id, job.status_message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
        return outcome


@celery_app.task
def sync_mail_source(source_id: str):
    from app.models.entities import Source
    from app.services.analysis import queue_source_analysis

    with SessionLocal() as db:
        source = db.get(Source, uuid.UUID(source_id))
        if not source or source.status != "active":
            return {"imported": 0, "status": "inactive"}
        user = db.get(User, source.user_id)
        imported = EmailService(db).sync(user, source.id)
        analysis = queue_source_analysis(db, user, source.id, sync_external=False)
        return {"imported": imported, "status": "completed", "analysis": analysis}


@celery_app.task
def schedule_tick():
    with SessionLocal() as db:
        fired = ReminderService(db).fire_due()
        notifications = NotificationService(db)
        briefings = notifications.enqueue_briefings()
        sent = notifications.deliver_pending()
        events = CalendarService(db).sync_pending()
        mail = EmailService(db).sync_all()
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
