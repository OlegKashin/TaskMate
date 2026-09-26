import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.models.entities import (
    AIProcessingJob,
    CalendarEvent,
    InboxItem,
    Task,
)
from app.schemas.domain import (
    AnalyzeRequest,
    ConfirmationToken,
    EmailReply,
    EventCreate,
    EventPatch,
    FolderPatch,
    IMAPCredentials,
    InboxSnooze,
    ProjectCreate,
    ProjectPatch,
    ReminderCreate,
    ReminderPatch,
    SourceCreate,
    SourcePatch,
    TaskCreate,
    TaskPatch,
    UserPatch,
    WaitingCreate,
    WaitingPatch,
)
from app.services.domain import (
    CalendarService,
    InboxService,
    NotificationService,
    ProjectService,
    ReminderService,
    SourceService,
    TaskService,
    UserService,
    WaitingService,
)
from app.services.email import EmailService

router = APIRouter(prefix="/api/v1")


def ok(data, meta=None):
    return {"data": jsonable_encoder(data), "meta": meta or {}}


@router.get("/me")
def me(user: CurrentUser):
    return ok(user)


@router.patch("/me")
def patch_me(payload: UserPatch, db: DB, user: CurrentUser):
    return ok(UserService(db).patch(user, payload.model_dump(exclude_unset=True)))


@router.get("/projects")
def projects(
    db: DB, user: CurrentUser, limit: int = Query(20, le=100), offset: int = Query(0, ge=0)
):
    rows, meta = ProjectService(db).list(user, limit, offset)
    return ok(rows, meta)


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, db: DB, user: CurrentUser):
    return ok(ProjectService(db).create(user, payload.model_dump()))


@router.get("/projects/{object_id}")
def project(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(ProjectService(db).get(user, object_id))


@router.patch("/projects/{object_id}")
def patch_project(object_id: uuid.UUID, payload: ProjectPatch, db: DB, user: CurrentUser):
    return ok(ProjectService(db).patch(user, object_id, payload.model_dump(exclude_unset=True)))


@router.delete("/projects/{object_id}", status_code=204)
def delete_project(object_id: uuid.UUID, db: DB, user: CurrentUser):
    ProjectService(db).delete(user, object_id)


@router.get("/tasks")
def tasks(
    db: DB,
    user: CurrentUser,
    status: str | None = None,
    project_id: uuid.UUID | None = None,
    priority: str | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    search: str | None = None,
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
):
    rows, meta = TaskService(db).list_filtered(
        user, limit, offset, status, project_id, priority, due_from, due_to, search
    )
    return ok(rows, meta)


@router.post("/tasks", status_code=201)
def create_task(payload: TaskCreate, db: DB, user: CurrentUser):
    task, undo = TaskService(db).create(user, payload.model_dump())
    return ok(task, {"undo_token": undo, "undo_ttl_seconds": 15})


@router.get("/tasks/{object_id}")
def task(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(TaskService(db).get(user, object_id))


@router.patch("/tasks/{object_id}")
def patch_task(object_id: uuid.UUID, payload: TaskPatch, db: DB, user: CurrentUser):
    task, undo = TaskService(db).patch(user, object_id, payload.model_dump(exclude_unset=True))
    return ok(task, {"undo_token": undo})


@router.delete("/tasks/{object_id}")
def delete_task(object_id: uuid.UUID, db: DB, user: CurrentUser):
    task, undo = TaskService(db).delete(user, object_id)
    return ok(task, {"undo_token": undo})


@router.post("/tasks/{object_id}/complete")
def complete_task(object_id: uuid.UUID, db: DB, user: CurrentUser):
    task, undo = TaskService(db).change_status(user, object_id, "completed")
    return ok(task, {"undo_token": undo})


@router.post("/tasks/{object_id}/cancel")
def cancel_task(object_id: uuid.UUID, db: DB, user: CurrentUser):
    task, undo = TaskService(db).change_status(user, object_id, "cancelled")
    return ok(task, {"undo_token": undo})


@router.post("/tasks/{object_id}/undo")
def undo_task(object_id: uuid.UUID, token: str, db: DB, user: CurrentUser):
    return ok(TaskService(db).undo(user, token, expected_task_id=object_id))


@router.get("/sources")
def sources(
    db: DB, user: CurrentUser, limit: int = Query(20, le=100), offset: int = Query(0, ge=0)
):
    rows, meta = SourceService(db).list(user, limit, offset)
    return ok(rows, meta)


@router.post("/sources", status_code=201)
def create_source(payload: SourceCreate, db: DB, user: CurrentUser):
    return ok(SourceService(db).create(user, payload.model_dump()))


@router.get("/sources/{object_id}")
def source(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(SourceService(db).get(user, object_id))


@router.patch("/sources/{object_id}")
def patch_source(object_id: uuid.UUID, payload: SourcePatch, db: DB, user: CurrentUser):
    return ok(SourceService(db).patch(user, object_id, payload.model_dump(exclude_unset=True)))


@router.delete("/sources/{object_id}")
def disconnect_source(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(SourceService(db).disconnect(user, object_id))


@router.get("/sources/{object_id}/folders")
def folders(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(SourceService(db).folders(user, object_id))


@router.patch("/sources/{object_id}/folders")
def patch_folders(object_id: uuid.UUID, payload: FolderPatch, db: DB, user: CurrentUser):
    return ok(
        SourceService(db).patch_folders(user, object_id, [x.model_dump() for x in payload.folders])
    )


@router.post("/sources/{object_id}/imap-credentials")
def configure_imap(object_id: uuid.UUID, payload: IMAPCredentials, db: DB, user: CurrentUser):
    return ok(EmailService(db).configure_imap(user, object_id, payload.username, payload.password))


@router.get("/inbox")
def inbox(
    db: DB,
    user: CurrentUser,
    status: str | None = None,
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
):
    filters = (InboxItem.status == status,) if status else ()
    rows, meta = InboxService(db).list(user, limit, offset, *filters)
    return ok(rows, meta)


@router.get("/inbox/{object_id}")
def inbox_item(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(InboxService(db).get(user, object_id))


@router.post("/inbox/{object_id}/resolve")
def resolve_inbox(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(InboxService(db).transition(user, object_id, "resolved"))


@router.post("/inbox/{object_id}/ignore")
def ignore_inbox(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(InboxService(db).transition(user, object_id, "ignored"))


@router.post("/inbox/{object_id}/snooze")
def snooze_inbox(object_id: uuid.UUID, payload: InboxSnooze, db: DB, user: CurrentUser):
    return ok(InboxService(db).transition(user, object_id, "snoozed", payload.until))


@router.post("/inbox/{object_id}/reply")
def reply_inbox(object_id: uuid.UUID, payload: EmailReply, db: DB, user: CurrentUser):
    return ok({"confirmation_token": InboxService(db).reply(user, object_id, payload.draft_text)})


@router.post("/inbox/replies/confirm")
def confirm_email_reply(payload: ConfirmationToken, db: DB, user: CurrentUser):
    return ok(
        {
            "status": "sent",
            "external_message_id": EmailService(db).confirm_reply(user, payload.token),
        }
    )


@router.post("/sources/{object_id}/sync")
def sync_email_source(object_id: uuid.UUID, db: DB, user: CurrentUser):
    EmailService(db)._source(user, object_id)
    from app.workers.tasks import sync_mail_source

    job = sync_mail_source.delay(str(object_id))
    return ok({"status": "queued", "job_id": job.id})


@router.get("/waiting-for")
def waiting_list(
    db: DB, user: CurrentUser, limit: int = Query(20, le=100), offset: int = Query(0, ge=0)
):
    rows, meta = WaitingService(db).list(user, limit, offset)
    return ok(rows, meta)


@router.post("/waiting-for", status_code=201)
def waiting_create(payload: WaitingCreate, db: DB, user: CurrentUser):
    return ok(WaitingService(db).create(user, payload.model_dump()))


@router.get("/waiting-for/{object_id}")
def waiting_get(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(WaitingService(db).get(user, object_id))


@router.patch("/waiting-for/{object_id}")
def waiting_patch(object_id: uuid.UUID, payload: WaitingPatch, db: DB, user: CurrentUser):
    return ok(WaitingService(db).patch(user, object_id, payload.model_dump(exclude_unset=True)))


@router.post("/waiting-for/{object_id}/complete")
def waiting_complete(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(WaitingService(db).transition(user, object_id, "completed"))


@router.post("/waiting-for/{object_id}/cancel")
def waiting_cancel(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(WaitingService(db).transition(user, object_id, "cancelled"))


@router.get("/reminders")
def reminder_list(
    db: DB, user: CurrentUser, limit: int = Query(20, le=100), offset: int = Query(0, ge=0)
):
    rows, meta = ReminderService(db).list(user, limit, offset)
    return ok(rows, meta)


@router.post("/reminders", status_code=201)
def reminder_create(payload: ReminderCreate, db: DB, user: CurrentUser):
    return ok(ReminderService(db).create(user, payload.model_dump()))


@router.get("/reminders/{object_id}")
def reminder_get(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(ReminderService(db).get(user, object_id))


@router.patch("/reminders/{object_id}")
def reminder_patch(object_id: uuid.UUID, payload: ReminderPatch, db: DB, user: CurrentUser):
    return ok(ReminderService(db).patch(user, object_id, payload.model_dump(exclude_unset=True)))


@router.post("/reminders/{object_id}/cancel")
def reminder_cancel(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(ReminderService(db).cancel(user, object_id))


@router.get("/calendar/events")
def events(db: DB, user: CurrentUser, limit: int = Query(20, le=100), offset: int = Query(0, ge=0)):
    rows, meta = CalendarService(db).list(user, limit, offset)
    return ok(rows, meta)


@router.post("/calendar/events", status_code=201)
def event_create(payload: EventCreate, db: DB, user: CurrentUser):
    return ok(CalendarService(db).create(user, payload.model_dump()))


@router.get("/calendar/events/{object_id}")
def event_get(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(CalendarService(db).get(user, object_id))


@router.patch("/calendar/events/{object_id}")
def event_patch(object_id: uuid.UUID, payload: EventPatch, db: DB, user: CurrentUser):
    return ok(CalendarService(db).patch(user, object_id, payload.model_dump(exclude_unset=True)))


@router.post("/calendar/events/{object_id}/cancel")
def event_cancel(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(CalendarService(db).cancel(user, object_id))


@router.get("/calendar/connections")
def connections(db: DB, user: CurrentUser):
    return ok(CalendarService(db).connections(user))


@router.delete("/calendar/connections/{object_id}")
def disconnect_calendar(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(CalendarService(db).disconnect(user, object_id))


@router.get("/notifications")
def notifications(
    db: DB, user: CurrentUser, limit: int = Query(20, le=100), offset: int = Query(0, ge=0)
):
    rows, meta = NotificationService(db).list(user, limit, offset)
    return ok(rows, meta)


@router.get("/notifications/{object_id}")
def notification(object_id: uuid.UUID, db: DB, user: CurrentUser):
    return ok(NotificationService(db).get(user, object_id))


@router.post("/analyze", status_code=202)
def analyze(payload: AnalyzeRequest, db: DB, user: CurrentUser):
    from app.services.analysis import queue_source_analysis

    return ok(queue_source_analysis(db, user, payload.source_id))


@router.get("/analyze/{job_id}")
def analyze_status(job_id: uuid.UUID, db: DB, user: CurrentUser):
    job = db.scalar(
        select(AIProcessingJob).where(
            AIProcessingJob.id == job_id, AIProcessingJob.user_id == user.id
        )
    )
    if not job:
        from app.core.errors import not_found

        raise not_found("ai_job")
    return ok(job)


@router.get("/today")
def today(db: DB, user: CurrentUser):
    now = datetime.now(UTC)
    end = now + timedelta(days=1)
    tasks = db.scalars(
        select(Task).where(
            Task.user_id == user.id,
            Task.deleted_at.is_(None),
            Task.due_at >= now,
            Task.due_at < end,
        )
    ).all()
    events = db.scalars(
        select(CalendarEvent).where(
            CalendarEvent.user_id == user.id,
            CalendarEvent.status != "cancelled",
            CalendarEvent.start_at >= now,
            CalendarEvent.start_at < end,
        )
    ).all()
    return ok({"tasks": tasks, "events": events})


@router.get("/stats")
def stats(db: DB, user: CurrentUser):
    created = (
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.user_id == user.id, Task.deleted_at.is_(None))
        )
        or 0
    )
    completed = (
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.user_id == user.id, Task.status == "completed", Task.deleted_at.is_(None))
        )
        or 0
    )
    return ok({"tasks": created, "completed": completed})
