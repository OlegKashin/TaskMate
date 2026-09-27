import uuid
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, conflict, not_found
from app.models.entities import (
    CalendarConnection,
    CalendarEvent,
    InboxItem,
    Message,
    Notification,
    Project,
    Reminder,
    Source,
    SourceCredential,
    SourceFolder,
    SourceProject,
    Task,
    TaskEvent,
    UIAction,
    User,
    UserSettings,
    WaitingFor,
    utcnow,
)

ModelT = TypeVar("ModelT")


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    return value


def pagination(limit: int, offset: int, total: int) -> dict[str, Any]:
    return {"pagination": {"limit": limit, "offset": offset, "total": total}}


def base36(number: int) -> str:
    if number < 0:
        raise ValueError("number must be non-negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    result = ""
    while number:
        number, remainder = divmod(number, 36)
        result = alphabet[remainder] + result
    return result


def from_base36(value: str) -> int:
    return int(value, 36)


class UserService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, telegram_user_id: int, **fields) -> User:
        user = self.db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user:
            return user
        user = User(telegram_user_id=telegram_user_id, **fields)
        self.db.add(user)
        self.db.flush()
        self.db.add(UserSettings(user_id=user.id))
        self.db.commit()
        return user

    def patch(self, user: User, values: dict[str, Any]) -> User:
        if "timezone" in values and values["timezone"]:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(values["timezone"])
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise AppError(
                    "VALIDATION_ERROR", f"Unknown timezone: {values['timezone']}", 422
                ) from exc
        for key, value in values.items():
            setattr(user, key, value)
        self.db.commit()
        return user


class OwnedService:
    model: type[ModelT]
    label: str

    def __init__(self, db: Session):
        self.db = db

    def get(self, user: User, object_id: uuid.UUID) -> ModelT:
        obj = self.db.scalar(
            select(self.model).where(self.model.id == object_id, self.model.user_id == user.id)
        )
        if not obj:
            raise not_found(self.label)
        return obj

    def list(self, user: User, limit: int, offset: int, *filters):
        where = (self.model.user_id == user.id, *filters)
        total = self.db.scalar(select(func.count()).select_from(self.model).where(*where)) or 0
        rows = self.db.scalars(select(self.model).where(*where).offset(offset).limit(limit)).all()
        return rows, pagination(limit, offset, total)


class ProjectService(OwnedService):
    model, label = Project, "project"

    def create(self, user: User, values: dict[str, Any]) -> Project:
        existing = self.db.scalar(
            select(Project).where(
                Project.user_id == user.id,
                Project.name == values["name"],
                Project.is_archived.is_(False),
            )
        )
        if existing:
            raise conflict(
                "PROJECT_NAME_CONFLICT", "An active project with this name already exists"
            )
        project = Project(user_id=user.id, **values)
        self.db.add(project)
        self.db.commit()
        return project

    def patch(self, user: User, project_id: uuid.UUID, values: dict[str, Any]) -> Project:
        project = self.get(user, project_id)
        if values.get("name") and values["name"] != project.name:
            duplicate = self.db.scalar(
                select(Project).where(
                    Project.user_id == user.id,
                    Project.name == values["name"],
                    Project.is_archived.is_(False),
                    Project.id != project.id,
                )
            )
            if duplicate:
                raise conflict(
                    "PROJECT_NAME_CONFLICT", "An active project with this name already exists"
                )
        for key, value in values.items():
            setattr(project, key, value)
        if project.is_archived:
            settings = self.db.get(UserSettings, user.id)
            if settings and settings.default_project_id == project.id:
                settings.default_project_id = None
        self.db.commit()
        return project

    def delete(self, user: User, project_id: uuid.UUID) -> None:
        project = self.get(user, project_id)
        settings = self.db.get(UserSettings, user.id)
        if settings and settings.default_project_id == project_id:
            settings.default_project_id = None
        self.db.execute(update(Task).where(
            Task.user_id == user.id, Task.project_id == project_id,
        ).values(project_id=None))
        for link in self.db.scalars(select(SourceProject).where(
            SourceProject.project_id == project_id,
        )).all():
            self.db.delete(link)
        self.db.delete(project)
        self.db.commit()


class TaskService(OwnedService):
    model, label = Task, "task"

    def get(self, user: User, object_id: uuid.UUID, include_deleted: bool = False) -> Task:
        conditions = [Task.id == object_id, Task.user_id == user.id]
        if not include_deleted:
            conditions.append(Task.deleted_at.is_(None))
        task = self.db.scalar(select(Task).where(*conditions))
        if not task:
            raise not_found("task")
        return task

    def _project(self, user: User, project_id: uuid.UUID | None) -> None:
        if project_id and not self.db.scalar(
            select(Project.id).where(
                Project.id == project_id, Project.user_id == user.id, Project.is_archived.is_(False)
            )
        ):
            raise not_found("project")

    def _event(
        self, task: Task, event_type: str, old: dict | None, new: dict | None, source="user_command"
    ):
        event = TaskEvent(
            task_id=task.id,
            user_id=task.user_id,
            event_type=event_type,
            old_value=old,
            new_value=new,
            source=source,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _undo(self, task: Task, event: TaskEvent) -> str:
        action = UIAction(
            user_id=task.user_id,
            action="undo_task",
            payload={"task_id": str(task.id), "event_id": str(event.id)},
            expires_at=utcnow() + timedelta(seconds=get_settings().undo_ttl_seconds),
        )
        self.db.add(action)
        self.db.flush()
        return f"a:{base36(action.id)}"

    def create(self, user: User, values: dict[str, Any], source="user_command") -> tuple[Task, str]:
        values = dict(values)
        if not values.get("project_id"):
            settings = self.db.get(UserSettings, user.id)
            if settings and settings.default_project_id:
                values["project_id"] = settings.default_project_id
        self._project(user, values.get("project_id"))
        if values.get("source_message_id"):
            if not self.db.scalar(select(Message.id).where(
                Message.id == values["source_message_id"], Message.user_id == user.id,
            )):
                raise not_found("message")
            duplicate = self.db.scalar(
                select(Task).where(
                    Task.user_id == user.id,
                    Task.source_message_id == values["source_message_id"],
                    Task.deleted_at.is_(None),
                    Task.status.not_in(["completed", "cancelled"]),
                )
            )
            if duplicate:
                raise conflict("DUPLICATE_TASK", "This message already created a task")
        task = Task(user_id=user.id, **values)
        self.db.add(task)
        self.db.flush()
        event = self._event(task, "created", None, {"deleted_at": None}, source)
        token = self._undo(task, event)
        self.db.commit()
        return task, token

    def list_filtered(
        self,
        user: User,
        limit=20,
        offset=0,
        status=None,
        project_id=None,
        priority=None,
        due_from=None,
        due_to=None,
        search=None,
    ):
        filters: list[Any] = [Task.deleted_at.is_(None)]
        for column, value in (
            (Task.status, status),
            (Task.project_id, project_id),
            (Task.priority, priority),
        ):
            if value is not None:
                filters.append(column == value)
        if due_from:
            filters.append(Task.due_at >= due_from)
        if due_to:
            filters.append(Task.due_at <= due_to)
        if search:
            filters.append(
                or_(Task.title.ilike(f"%{search}%"), Task.description.ilike(f"%{search}%"))
            )
        return self.list(user, limit, offset, *filters)

    def patch(
        self, user: User, task_id: uuid.UUID, values: dict[str, Any], source="user_command"
    ) -> tuple[Task, str]:
        task = self.get(user, task_id)
        self._project(user, values.get("project_id"))
        old = {key: json_value(getattr(task, key)) for key in values}
        for key, value in values.items():
            setattr(task, key, value)
        event = self._event(
            task, "updated", old, {key: json_value(value) for key, value in values.items()}, source
        )
        token = self._undo(task, event)
        self.db.commit()
        return task, token

    def change_status(self, user: User, task_id: uuid.UUID, status: str) -> tuple[Task, str]:
        if status not in {"new", "in_progress", "completed", "cancelled"}:
            raise AppError("VALIDATION_ERROR", "Invalid task status", 422)
        task = self.get(user, task_id)
        old = {
            "status": task.status,
            "completed_at": json_value(task.completed_at),
            "cancelled_at": json_value(task.cancelled_at),
        }
        task.status = status
        task.completed_at = utcnow() if status == "completed" else None
        task.cancelled_at = utcnow() if status == "cancelled" else None
        new = {
            "status": status,
            "completed_at": json_value(task.completed_at),
            "cancelled_at": json_value(task.cancelled_at),
        }
        event = self._event(task, "status_changed", old, new)
        token = self._undo(task, event)
        self.db.commit()
        return task, token

    def delete(self, user: User, task_id: uuid.UUID) -> tuple[Task, str]:
        task = self.get(user, task_id)
        old = {"deleted_at": None}
        task.deleted_at = utcnow()
        event = self._event(task, "deleted", old, {"deleted_at": json_value(task.deleted_at)})
        token = self._undo(task, event)
        self.db.commit()
        return task, token

    def undo(
        self,
        user: User,
        token: str,
        expected_task_id: uuid.UUID | None = None,
        consumed_action: UIAction | None = None,
    ) -> Task:
        action = consumed_action or UIActionService(self.db).consume(
            user, token, expected="undo_task"
        )
        if action.action != "undo_task":
            raise conflict("ACTION_TYPE_MISMATCH", "Unexpected action type")
        if expected_task_id and action.payload["task_id"] != str(expected_task_id):
            self.db.rollback()
            raise conflict("ACTION_TARGET_MISMATCH", "Undo token belongs to another task")
        task = self.get(user, uuid.UUID(action.payload["task_id"]), include_deleted=True)
        event = self.db.scalar(
            select(TaskEvent).where(
                TaskEvent.id == uuid.UUID(action.payload["event_id"]), TaskEvent.user_id == user.id
            )
        )
        if not event:
            raise not_found("task_event")
        if event.event_type == "created":
            task.deleted_at = utcnow()
        else:
            for key, value in (event.old_value or {}).items():
                if key.endswith("_at") and value:
                    value = datetime.fromisoformat(value)
                elif key.endswith("_id") and value:
                    value = uuid.UUID(value)
                setattr(task, key, value)
        self._event(task, "undo", event.new_value, event.old_value, "undo")
        self.db.commit()
        return task

    def events(self, user: User, task_id: uuid.UUID) -> list[TaskEvent]:
        self.get(user, task_id, include_deleted=True)
        return list(
            self.db.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id, TaskEvent.user_id == user.id)
                .order_by(TaskEvent.created_at.asc())
            ).all()
        )


class UIActionService:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self, user: User, action: str, payload: dict, ttl_seconds=900,
        *, commit: bool = True,
    ) -> str:
        item = UIAction(
            user_id=user.id,
            action=action,
            payload=payload,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
        self.db.add(item)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return f"a:{base36(item.id)}"

    def consume(self, user: User, token: str, expected: str | None = None) -> UIAction:
        try:
            prefix, encoded = token.split(":", 1)
            action_id = from_base36(encoded)
        except (ValueError, TypeError):
            raise AppError("INVALID_ACTION", "Invalid action token", 400) from None
        if prefix != "a":
            raise AppError("INVALID_ACTION", "Invalid action token", 400)
        now = utcnow()
        result = self.db.execute(
            update(UIAction)
            .where(
                UIAction.id == action_id,
                UIAction.user_id == user.id,
                UIAction.consumed_at.is_(None),
                UIAction.expires_at > now,
            )
            .values(consumed_at=now)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise conflict("ACTION_EXPIRED_OR_USED", "Action expired or was already used")
        item = self.db.get(UIAction, action_id)
        if expected and item.action != expected:
            self.db.rollback()
            raise conflict("ACTION_TYPE_MISMATCH", "Unexpected action type")
        return item


class SourceService(OwnedService):
    model, label = Source, "source"

    def projects(self, user: User, source_id: uuid.UUID) -> list[Project]:
        self.get(user, source_id)
        return self.db.scalars(select(Project).join(SourceProject).where(
            SourceProject.source_id == source_id,
            Project.user_id == user.id,
            Project.is_archived.is_(False),
        ).order_by(Project.name)).all()

    def replace_projects(
        self, user: User, source_id: uuid.UUID, project_ids: list[uuid.UUID]
    ) -> list[Project]:
        self.get(user, source_id)
        unique_ids = list(dict.fromkeys(project_ids))
        projects = [ProjectService(self.db).get(user, project_id) for project_id in unique_ids]
        if any(project.is_archived for project in projects):
            raise AppError("PROJECT_ARCHIVED", "Archived project cannot be linked", 422)
        for link in self.db.scalars(select(SourceProject).where(
            SourceProject.source_id == source_id
        )).all():
            self.db.delete(link)
        self.db.flush()
        self.db.add_all(SourceProject(source_id=source_id, project_id=project.id)
                        for project in projects)
        self.db.commit()
        return projects

    def create(self, user: User, values: dict[str, Any]) -> Source:
        if values["type"] in {"gmail", "yandex", "mailru"}:
            raise AppError("OAUTH_REQUIRED", "Connect this mailbox through OAuth", 422)
        is_imap = values["type"] == "imap"
        existing = None
        if values.get("external_source_id"):
            existing = self.db.scalar(
                select(Source).where(
                    Source.user_id == user.id,
                    Source.type == values["type"],
                    Source.external_source_id == values["external_source_id"],
                )
            )
        now = utcnow()
        if existing:
            if existing.status != "disconnected":
                raise conflict("SOURCE_ALREADY_CONNECTED", "Source is already connected")
            existing.name = values["name"]
            existing.status = "connecting" if is_imap else "active"
            existing.connected_at = existing.last_analyzed_at = existing.last_synced_at = (
                None if is_imap else now
            )
            self.db.commit()
            return existing
        source = Source(
            user_id=user.id,
            status="connecting" if is_imap else "active",
            connected_at=None if is_imap else now,
            last_analyzed_at=None if is_imap else now,
            last_synced_at=None if is_imap else now,
            **values,
        )
        self.db.add(source)
        self.db.commit()
        return source

    def patch(self, user: User, source_id: uuid.UUID, values: dict[str, Any]) -> Source:
        source = self.get(user, source_id)
        if source.status == "disconnected":
            raise conflict("SOURCE_DISCONNECTED", "Reconnect this source instead of editing it")
        if (
            source.type in {"gmail", "yandex", "mailru", "imap"}
            and values.get("status") == "active"
        ):
            credential = self.db.scalar(
                select(SourceCredential).where(SourceCredential.source_id == source.id)
            )
            if not credential or not (
                credential.encrypted_access_token or credential.encrypted_password
            ):
                raise AppError("MAIL_NOT_CONFIGURED", "Mailbox credentials are missing", 422)
        for key, value in values.items():
            setattr(source, key, value)
        self.db.commit()
        return source

    def disconnect(self, user: User, source_id: uuid.UUID) -> Source:
        source = self.get(user, source_id)
        source.status = "disconnected"
        credential = self.db.scalar(
            select(SourceCredential).where(SourceCredential.source_id == source.id)
        )
        if credential:
            for field in (
                "encrypted_access_token",
                "encrypted_refresh_token",
                "encrypted_username",
                "encrypted_password",
            ):
                setattr(credential, field, None)
        self.db.commit()
        return source

    def folders(self, user: User, source_id: uuid.UUID) -> list[SourceFolder]:
        source = self.get(user, source_id)
        return list(
            self.db.scalars(select(SourceFolder).where(SourceFolder.source_id == source.id))
        )

    def patch_folders(
        self, user: User, source_id: uuid.UUID, values: list[dict]
    ) -> list[SourceFolder]:
        folders = {item.external_folder_id: item for item in self.folders(user, source_id)}
        for value in values:
            if value["external_folder_id"] not in folders:
                raise AppError(
                    "VALIDATION_ERROR", f"Unknown folder: {value['external_folder_id']}", 422
                )
            folders[value["external_folder_id"]].is_selected = value["is_selected"]
        if folders and not any(folder.is_selected for folder in folders.values()):
            self.db.rollback()
            raise AppError("VALIDATION_ERROR", "Select at least one folder", 422)
        self.db.commit()
        return list(folders.values())


class InboxService(OwnedService):
    model, label = InboxItem, "inbox_item"

    def transition(
        self, user: User, item_id: uuid.UUID, status: str, until: datetime | None = None
    ) -> InboxItem:
        item = self.get(user, item_id)
        item.status = status
        item.resolved_at = utcnow() if status in {"resolved", "ignored"} else None
        item.snoozed_until = until if status == "snoozed" else None
        self.db.commit()
        return item

    def wake_due_snoozed(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        items = self.db.scalars(
            select(InboxItem).where(
                InboxItem.status == "snoozed",
                InboxItem.snoozed_until <= now,
            )
        ).all()
        for item in items:
            item.status = "proposed"
            item.snoozed_until = None
        if items:
            self.db.commit()
        return len(items)

    def reply(self, user: User, item_id: uuid.UUID, draft_text: str) -> str:
        item = self.get(user, item_id)
        message = self.db.get(Message, item.message_id) if item.message_id else None
        if not message or message.user_id != user.id or message.message_type != "email":
            raise conflict(
                "REPLY_NOT_SUPPORTED_FOR_SOURCE", "Reply is supported only for email messages"
            )
        from app.services.email import EmailService

        return EmailService(self.db).draft_for_message(user, message.id, draft_text)


class SimpleOwnedService(OwnedService):
    def create(self, user: User, values: dict[str, Any]):
        obj = self.model(user_id=user.id, **values)
        self.db.add(obj)
        self.db.commit()
        return obj

    def patch(self, user: User, object_id: uuid.UUID, values: dict[str, Any]):
        obj = self.get(user, object_id)
        for key, value in values.items():
            setattr(obj, key, value)
        self.db.commit()
        return obj


class WaitingService(SimpleOwnedService):
    model, label = WaitingFor, "waiting_for"

    def create(self, user: User, values: dict[str, Any]) -> WaitingFor:
        if values.get("message_id") and not self.db.scalar(select(Message.id).where(
            Message.id == values["message_id"], Message.user_id == user.id,
        )):
            raise not_found("message")
        return super().create(user, values)

    def transition(self, user, object_id, status):
        obj = self.get(user, object_id)
        obj.status = status
        obj.completed_at = utcnow() if status == "completed" else None
        obj.cancelled_at = utcnow() if status == "cancelled" else None
        self.db.commit()
        return obj


class ReminderService(SimpleOwnedService):
    model, label = Reminder, "reminder"

    def create(self, user: User, values: dict[str, Any]) -> Reminder:
        if values.get("task_id"):
            TaskService(self.db).get(user, values["task_id"])
        return super().create(user, values)

    def cancel(self, user, object_id):
        obj = self.get(user, object_id)
        if obj.status != "pending":
            raise conflict("REMINDER_NOT_PENDING", "Only pending reminders can be cancelled")
        obj.status, obj.cancelled_at = "cancelled", utcnow()
        self.db.commit()
        return obj

    def fire_due(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        reminders = self.db.scalars(
            select(Reminder).where(Reminder.status == "pending", Reminder.due_at <= now)
        ).all()
        for reminder in reminders:
            reminder.status, reminder.fired_at = "fired", now
            key = f"reminder:{reminder.id}"
            if not self.db.scalar(select(Notification).where(Notification.dedupe_key == key)):
                self.db.add(
                    Notification(
                        user_id=reminder.user_id,
                        type="task_reminder",
                        payload={"reminder_id": str(reminder.id), "title": reminder.title},
                        dedupe_key=key,
                    )
                )
        self.db.commit()
        return len(reminders)


class NotificationService(OwnedService):
    model, label = Notification, "notification"

    def enqueue_briefings(self, now: datetime | None = None) -> int:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        now = now or utcnow()
        created = 0
        for user, settings in self.db.execute(
            select(User, UserSettings)
            .join(UserSettings, UserSettings.user_id == User.id)
            .where(User.is_active.is_(True))
        ):
            try:
                local = now.astimezone(ZoneInfo(user.timezone))
            except ZoneInfoNotFoundError:
                continue
            for kind, enabled, scheduled in (
                (
                    "morning_briefing",
                    settings.morning_briefing_enabled,
                    settings.morning_briefing_time,
                ),
                ("evening_stats", settings.evening_stats_enabled, settings.evening_stats_time),
            ):
                scheduled_minutes = scheduled.hour * 60 + scheduled.minute
                elapsed = local.hour * 60 + local.minute - scheduled_minutes
                if not enabled or not 0 <= elapsed < 15:
                    continue
                key = f"{kind}:{user.id}:{local.date()}"
                if self.db.scalar(select(Notification.id).where(Notification.dedupe_key == key)):
                    continue
                self.db.add(Notification(user_id=user.id, type=kind, payload={}, dedupe_key=key))
                created += 1
        self.db.commit()
        return created

    def deliver_pending(self, client=None) -> int:
        from app.bot.presentation import section_message
        from app.integrations.telegram.client import TelegramClient

        client = client or TelegramClient()
        sent = 0
        notification_ids = self.db.scalars(
            select(Notification.id)
            .where(Notification.status == "pending")
            .order_by(Notification.created_at)
            .limit(100)
        ).all()
        for notification_id in notification_ids:
            notification = self.db.scalar(
                select(Notification)
                .where(Notification.id == notification_id, Notification.status == "pending")
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if not notification:
                self.db.commit()
                continue
            user = self.db.get(User, notification.user_id)
            if not user or not user.is_active:
                self.db.commit()
                continue
            markup = None
            if notification.type == "morning_briefing":
                body = section_message(self.db, user, "today")
                t_token = UIActionService(self.db).create(
                    user, "navigate", {"section": "tasks"}, ttl_seconds=86400, commit=False
                )
                s_token = UIActionService(self.db).create(
                    user, "navigate", {"section": "schedule"}, ttl_seconds=86400, commit=False
                )
                markup = {"inline_keyboard": [[
                    {"text": "Открыть задачи", "callback_data": t_token},
                    {"text": "Расписание", "callback_data": s_token},
                ]]}
            elif notification.type == "evening_stats":
                body = section_message(self.db, user, "stats")
                t_token = UIActionService(self.db).create(
                    user, "navigate", {"section": "tasks"}, ttl_seconds=86400, commit=False
                )
                cfg_token = UIActionService(self.db).create(
                    user, "navigate", {"section": "settings"}, ttl_seconds=86400, commit=False
                )
                markup = {"inline_keyboard": [[
                    {"text": "Задачи на завтра", "callback_data": t_token},
                    {"text": "Настройки", "callback_data": cfg_token},
                ]]}
            elif notification.type == "urgent_item":
                body = notification.payload.get("title") or notification.type
                msg_id = notification.payload.get("message_id")
                inbox_item = None
                if msg_id:
                    inbox_item = self.db.scalar(
                        select(InboxItem).where(
                            InboxItem.user_id == user.id, InboxItem.message_id == uuid.UUID(msg_id)
                        )
                    )
                buttons = []
                if inbox_item:
                    task_token = UIActionService(self.db).create(
                        user, "inbox_action", {"id": str(inbox_item.id), "operation": "create_task"},
                        ttl_seconds=86400, commit=False,
                    )
                    buttons.append({"text": "Создать задачу", "callback_data": task_token})
                    inbox_token = UIActionService(self.db).create(
                        user, "inbox_open", {"id": str(inbox_item.id)}, ttl_seconds=86400, commit=False
                    )
                    buttons.append({"text": "В Inbox", "callback_data": inbox_token})
                else:
                    inbox_sec_token = UIActionService(self.db).create(
                        user, "navigate", {"section": "inbox"}, ttl_seconds=86400, commit=False
                    )
                    buttons.append({"text": "В Inbox", "callback_data": inbox_sec_token})
                markup = {"inline_keyboard": [buttons]}
            elif notification.type == "calendar_error" and notification.payload.get("event_id"):
                body = notification.payload.get("title") or notification.type
                event = self.db.get(CalendarEvent, uuid.UUID(notification.payload["event_id"]))
                if event and event.user_id == user.id:
                    if self.db.get(CalendarConnection, event.connection_id).status == "active":
                        label, action, payload = "Повторить", "calendar_retry", {"id": str(event.id)}
                    else:
                        label, action, payload = "Переподключить", "connect_provider", {"provider": "google"}
                    token = UIActionService(self.db).create(
                        user, action, payload, ttl_seconds=86400, commit=False,
                    )
                    markup = {"inline_keyboard": [[
                        {"text": label, "callback_data": token},
                    ]]}
            else:
                body = notification.payload.get("title") or notification.type
            try:
                response = (client.send_message(user.telegram_user_id, body, markup)
                            if markup else client.send_message(user.telegram_user_id, body))
            except Exception:
                # Leave it pending for the next scheduler tick.
                self.db.rollback()
                continue
            if not response:
                self.db.rollback()
                continue
            notification.status = "sent"
            notification.sent_at = utcnow()
            self.db.commit()
            sent += 1
        return sent


class CalendarService(OwnedService):
    model, label = CalendarEvent, "calendar_event"

    def connections(self, user: User):
        return list(
            self.db.scalars(select(CalendarConnection).where(CalendarConnection.user_id == user.id))
        )

    def create(self, user: User, values: dict[str, Any]) -> CalendarEvent:
        connection = self.db.scalar(
            select(CalendarConnection).where(
                CalendarConnection.id == values["connection_id"],
                CalendarConnection.user_id == user.id,
            )
        )
        if not connection:
            raise not_found("calendar_connection")
        if connection.status != "active":
            raise conflict("CALENDAR_DISCONNECTED", "Calendar connection is not active")
        if values.get("task_id"):
            TaskService(self.db).get(user, values["task_id"])
        if values.get("task_id") and self.db.scalar(
            select(CalendarEvent).where(
                CalendarEvent.task_id == values["task_id"], CalendarEvent.status != "cancelled"
            )
        ):
            raise conflict(
                "DUPLICATE_CALENDAR_EVENT", "An active event already exists for this task"
            )
        event = CalendarEvent(user_id=user.id, status="pending", **values)
        self.db.add(event)
        self.db.commit()
        return event

    def patch(self, user, object_id, values):
        event = self.get(user, object_id)
        for key, value in values.items():
            setattr(event, key, value)
        if event.end_at <= event.start_at:
            raise AppError("VALIDATION_ERROR", "end_at must be after start_at", 422)
        event.status = "pending"
        event.sync_attempts = 0
        event.next_sync_at = None
        self.db.commit()
        return event

    def sync_pending(self, limit: int = 25) -> int:
        from app.integrations.calendar import GoogleCalendarAPI

        api = GoogleCalendarAPI(self.db)
        synced = 0
        now = utcnow()
        events = self.db.scalars(
            select(CalendarEvent)
            .where(
                CalendarEvent.status == "pending",
                CalendarEvent.sync_attempts < 4,
                or_(CalendarEvent.next_sync_at.is_(None), CalendarEvent.next_sync_at <= now),
            )
            .order_by(CalendarEvent.created_at)
            .limit(limit)
        ).all()
        for event in events:
            connection = self.db.get(CalendarConnection, event.connection_id)
            if not connection or connection.status != "active":
                continue
            try:
                event.external_event_id = api.save(connection, event)
            except AppError as exc:
                if exc.code == "CALENDAR_RECONNECT_REQUIRED":
                    connection.status = "error"
                    event.sync_attempts = 4
                else:
                    delay = (30, 120, 600)
                    event.sync_attempts += 1
                    event.next_sync_at = (
                        now + timedelta(seconds=delay[event.sync_attempts - 1])
                        if event.sync_attempts <= 3 else None
                    )
                if event.sync_attempts >= 4:
                    self.db.add(Notification(
                        user_id=event.user_id, type="calendar_error",
                        payload={"event_id": str(event.id), "title": f"Не удалось синхронизировать событие «{event.title}». Проверьте календарь и повторите отправку."},
                    ))
                self.db.commit()
                continue
            event.status = "confirmed"
            event.sync_attempts = 0
            event.next_sync_at = None
            self.db.commit()
            synced += 1
        return synced

    def retry(self, user: User, event_id: uuid.UUID) -> CalendarEvent:
        event = self.get(user, event_id)
        if event.status != "pending":
            raise conflict("CALENDAR_NOT_PENDING", "Only pending events can be retried")
        connection = self.db.get(CalendarConnection, event.connection_id)
        if not connection or connection.status != "active":
            raise conflict("CALENDAR_DISCONNECTED", "Reconnect Google Calendar first")
        event.sync_attempts = 0
        event.next_sync_at = None
        self.db.commit()
        return event

    def cancel(self, user, object_id):
        event = self.get(user, object_id)
        if event.status == "cancelled":
            return event
        if event.external_event_id:
            connection = self.db.get(CalendarConnection, event.connection_id)
            if not connection or connection.status != "active":
                raise conflict("CALENDAR_DISCONNECTED", "Calendar connection is not active")
            from app.integrations.calendar import GoogleCalendarAPI

            GoogleCalendarAPI(self.db).delete(connection, event)
        event.status = "cancelled"
        event.next_sync_at = None
        self.db.commit()
        return event

    def disconnect(self, user: User, connection_id: uuid.UUID):
        connection = self.db.scalar(
            select(CalendarConnection).where(
                CalendarConnection.id == connection_id, CalendarConnection.user_id == user.id
            )
        )
        if not connection:
            raise not_found("calendar_connection")
        connection.status = "disconnected"
        connection.encrypted_access_token = connection.encrypted_refresh_token = None
        self.db.commit()
        return connection
