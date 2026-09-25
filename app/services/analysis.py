"""Queue analysis for stored source messages without inventing no-op jobs."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.entities import AIProcessingJob, Message, Source, User


def queue_source_analysis(db: Session, user: User, source_id: uuid.UUID | None = None,
                          *, sync_external: bool = True) -> dict:
    query = select(Source).where(Source.user_id == user.id, Source.status == "active")
    if source_id:
        query = query.where(Source.id == source_id)
    sources = db.scalars(query).all()
    if not sources:
        if source_id:
            raise AppError("SOURCE_NOT_FOUND", "Active source not found", 404)
        return {"status": "no_sources", "queued": 0}
    mail_sources = [source for source in sources if source.type in {"gmail", "yandex", "mailru", "imap"}]
    mail_jobs = 0
    if sync_external:
        from app.workers.tasks import sync_mail_source

        for source in mail_sources:
            try:
                sync_mail_source.delay(str(source.id))
                mail_jobs += 1
            except Exception:
                pass
    source_ids = [source.id for source in sources]
    messages = db.scalars(select(Message).where(
        Message.user_id == user.id,
        Message.source_id.in_(source_ids),
        Message.processing_status.in_(["received", "failed"]),
    ).limit(100)).all()
    job_ids = []
    for message in messages:
        if not message.text and not message.subject:
            continue
        job = db.scalar(select(AIProcessingJob).where(
            AIProcessingJob.message_id == message.id,
            AIProcessingJob.status == "failed",
        ).order_by(AIProcessingJob.created_at.desc()))
        if job:
            job.status = "queued"
        else:
            job = AIProcessingJob(user_id=user.id, message_id=message.id, job_type="interpret")
            db.add(job)
            db.flush()
        message.processing_status = "queued"
        job_ids.append(job.id)
    db.commit()
    from app.workers.tasks import process_message

    for job_id in job_ids:
        try:
            process_message.delay(str(job_id))
        except Exception:
            # Persisted jobs remain visible and can be retried when the broker recovers.
            pass
    return {
        "status": "queued" if job_ids else "completed",
        "queued": len(job_ids),
        "external_sync": ("queued" if mail_jobs else "broker_unavailable") if mail_sources and sync_external
                         else ("not_requested" if mail_sources else "not_applicable"),
        "mail_jobs": mail_jobs,
    }
