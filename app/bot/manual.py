"""Owner-bound Telegram forms for manual task creation."""

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.bot.presentation import outcome_message
from app.bot.views import _button
from app.core.errors import AppError
from app.models.entities import Project, UserSettings
from app.services.domain import TaskService, UIActionService


def task_draft_view(db, user, draft: dict) -> tuple[str, dict]:
    project_id = draft.get("project_id")
    project = db.get(Project, uuid.UUID(project_id)) if project_id else None
    due_at = draft.get("due_at")
    text = (
        f"{'Изменение задачи' if draft.get('task_id') else 'Новая задача'}\n{draft['title']}\n"
        f"Срок: {due_at or 'не указан'}\n"
        f"Проект: {project.name if project else 'Inbox'}\n"
        f"Приоритет: {draft.get('priority', 'normal')}"
    )
    rows = [
        [_button(db, user, "Сохранить" if draft.get("task_id") else "Создать",
                 "task_draft_create", {"draft": draft})],
        [_button(db, user, "Срок", "task_draft_due", {"draft": draft}),
         _button(db, user, "Проект", "task_draft_project", {"draft": draft})],
        [_button(db, user, "Приоритет", "task_draft_priority", {"draft": draft}),
         _button(db, user, "Отмена", "cancel_ai", {})],
    ]
    return text, {"inline_keyboard": rows}


def new_task_draft(db, user, title: str) -> tuple[str, dict]:
    title = title.strip()
    if not title or len(title) > 500:
        raise AppError("VALIDATION_ERROR", "Укажите название задачи до 500 символов", 422)
    settings = db.get(UserSettings, user.id)
    project_id = str(settings.default_project_id) if settings and settings.default_project_id else None
    return task_draft_view(db, user, {
        "title": title, "project_id": project_id, "priority": "normal",
    })


def edit_task_draft(db, user, task_id: uuid.UUID) -> tuple[str, dict]:
    task = TaskService(db).get(user, task_id)
    return task_draft_view(db, user, {
        "task_id": str(task.id), "title": task.title,
        "project_id": str(task.project_id) if task.project_id else None,
        "priority": task.priority,
        "due_at": task.due_at.isoformat() if task.due_at else None,
    })


def handle_task_draft_action(db, user, kind: str, draft: dict, payload: dict):
    if kind == "task_draft_create":
        values = dict(draft)
        values["project_id"] = uuid.UUID(values["project_id"]) if values.get("project_id") else None
        if values.get("due_at"):
            values["due_at"] = datetime.fromisoformat(values["due_at"])
        task_id = values.pop("task_id", None)
        if task_id:
            task, undo = TaskService(db).patch(user, uuid.UUID(task_id), values)
        else:
            task, undo = TaskService(db).create(user, values)
        suggest_cal = False
        if task.due_at and (task.due_at.hour != 0 or task.due_at.minute != 0):
            from app.models.entities import CalendarConnection, CalendarEvent
            has_cal = db.scalar(select(CalendarConnection.id).where(
                CalendarConnection.user_id == user.id, CalendarConnection.status == "active"
            ))
            if has_cal:
                has_event = db.scalar(select(CalendarEvent.id).where(
                    CalendarEvent.user_id == user.id, CalendarEvent.task_id == task.id,
                    CalendarEvent.status != "cancelled",
                ))
                if not has_event:
                    suggest_cal = True
        return outcome_message(db, user, {
            "state": "executed", "object_id": str(task.id), "undo": undo,
            "suggest_calendar": suggest_cal,
        })
    if kind == "task_draft_due":
        token = UIActionService(db).create(user, "task_due_input", {"draft": draft})
        return (f"Укажите срок: /task_due {token} ГГГГ-ММ-ДД ЧЧ:ММ\n"
                "Время будет истолковано в вашем часовом поясе."), None
    if kind == "task_draft_project":
        projects = db.scalars(select(Project).where(
            Project.user_id == user.id, Project.is_archived.is_(False),
        ).order_by(Project.name).limit(50)).all()
        rows = [[_button(db, user, "Inbox", "task_draft_set_project", {
            "draft": draft, "project_id": None,
        })]]
        rows.extend([_button(db, user, project.name[:40], "task_draft_set_project", {
            "draft": draft, "project_id": str(project.id),
        })] for project in projects)
        return "К какому проекту отнести задачу?", {"inline_keyboard": rows}
    if kind == "task_draft_priority":
        rows = [[_button(db, user, label, "task_draft_set_priority", {
            "draft": draft, "priority": priority,
        })] for priority, label in (
            ("low", "🟢 Низкий"), ("normal", "🟡 Обычный"), ("high", "🔴 Высокий"),
        )]
        return "Выберите приоритет", {"inline_keyboard": rows}
    if kind == "task_draft_set_project":
        project_id = payload.get("project_id")
        if project_id:
            project = db.get(Project, uuid.UUID(project_id))
            if not project or project.user_id != user.id or project.is_archived:
                raise AppError("PROJECT_NOT_FOUND", "Project not found", 404)
        return task_draft_view(db, user, {**draft, "project_id": project_id})
    if kind == "task_draft_set_priority":
        priority = payload["priority"]
        if priority not in {"low", "normal", "high"}:
            raise AppError("VALIDATION_ERROR", "Invalid priority", 422)
        return task_draft_view(db, user, {**draft, "priority": priority})
    raise AppError("INVALID_ACTION", "Unknown task draft action", 400)


def set_task_due(db, user, token: str, raw_due_at: str) -> tuple[str, dict]:
    try:
        local = datetime.strptime(raw_due_at.strip(), "%Y-%m-%d %H:%M")
        zone = ZoneInfo(user.timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise AppError("VALIDATION_ERROR", "Используйте ГГГГ-ММ-ДД ЧЧ:ММ", 422) from exc
    action = UIActionService(db).consume(user, token, "task_due_input")
    draft = {**action.payload["draft"], "due_at": local.replace(tzinfo=zone).astimezone(UTC).isoformat()}
    return task_draft_view(db, user, draft)
