"""User-facing callback operations; domain services remain the source of truth."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.bot.manual import edit_task_draft, handle_task_draft_action
from app.bot.presentation import outcome_message
from app.bot.views import (
    _button,
    folders_view,
    inbox_view,
    object_view,
    section_view,
    source_projects_view,
)
from app.core.errors import AppError
from app.models.entities import CalendarConnection, Message, Project, UserSettings
from app.services.domain import (
    CalendarService,
    InboxService,
    ProjectService,
    ReminderService,
    SourceService,
    TaskService,
    UIActionService,
    WaitingService,
    utcnow,
)
from app.services.email import EmailService

ACTIONS = {
    "inbox_open", "tasks_open", "projects_open", "sources_open", "schedule_open",
    "calendar_retry", "calendar_cancel_prompt", "calendar_cancel", "inbox_action",
    "task_complete", "task_delete_prompt", "task_delete", "project_archive",
    "source_toggle", "source_disconnect_prompt", "source_disconnect",
    "folder_show", "folder_refresh", "folder_toggle", "setting_toggle",
    "setting_times", "setting_projects", "setting_project",
    "source_projects_view", "source_project_toggle",
    "task_new_prompt", "task_draft_create", "task_draft_due",
    "task_draft_project", "task_draft_priority", "task_draft_set_project",
    "task_draft_set_priority", "task_edit_prompt", "task_complete_prompt",
    "project_new_prompt", "project_create_confirm", "project_rename_prompt",
    "project_delete_prompt", "project_delete",
    "task_snooze", "task_add_calendar", "source_setting_toggle",
    "waiting_open", "waiting_complete", "waiting_cancel", "reminder_cancel",
    "cancel_ai", "edit_ai", "source_connect_menu", "setting_timezone",
    "task_disambiguate_edit", "project_disambiguate_rename",
}


def handle_action(db, user, action) -> tuple[str, dict | None]:
    kind, payload = action.action, action.payload
    if kind == "task_new_prompt":
        return "Напишите: /task <название задачи>", None
    if kind.startswith("task_draft_"):
        return handle_task_draft_action(db, user, kind, payload["draft"], payload)
    if kind == "project_new_prompt":
        return "Напишите: /project <название проекта>", None
    if kind == "project_create_confirm":
        project = ProjectService(db).create(user, {"name": payload["name"]})
        return f"Проект «{project.name}» создан.", None
    if kind == "inbox_open":
        return inbox_view(db, user, uuid.UUID(payload["id"]))
    if kind in {"tasks_open", "projects_open", "sources_open", "schedule_open"}:
        return object_view(db, user, kind.removesuffix("_open"), uuid.UUID(payload["id"]))
    if kind == "calendar_retry":
        try:
            CalendarService(db).retry(user, uuid.UUID(payload["id"]))
        except AppError as exc:
            return exc.message, None
        return "Повторная синхронизация запланирована.", None
    if kind == "calendar_cancel_prompt":
        event = CalendarService(db).get(user, uuid.UUID(payload["id"]))
        if event.status == "cancelled":
            return "Событие уже отменено.", None
        return (f"Отменить событие «{event.title}»?\n"
                "Оно будет отменено и в Google Calendar. Это действие нельзя отменить через Undo."), {"inline_keyboard": [[
            _button(db, user, "Да, отменить событие", "calendar_cancel", {"id": str(event.id)}),
            _button(db, user, "Не отменять", "schedule_open", {"id": str(event.id)}),
        ]]}
    if kind == "calendar_cancel":
        try:
            event = CalendarService(db).cancel(user, uuid.UUID(payload["id"]))
        except AppError as exc:
            if exc.code == "CALENDAR_DISCONNECTED":
                return "Подключение Google Calendar требует переподключения.", {"inline_keyboard": [[
                    _button(db, user, "Переподключить", "connect_provider", {"provider": "google"}),
                ]]}
            return "Не удалось отменить событие в Google Calendar. Попробуйте ещё раз.", {"inline_keyboard": [[
                _button(db, user, "Повторить", "calendar_cancel", {"id": payload["id"]}),
                _button(db, user, "Не сейчас", "schedule_open", {"id": payload["id"]}),
            ]]}
        return f"Событие «{event.title}» отменено.", None
    if kind == "inbox_action":
        item_id = uuid.UUID(payload["id"])
        item = InboxService(db).get(user, item_id)
        operation = payload["operation"]
        if operation == "clarify":
            if not item.message_id:
                return "Исходное сообщение недоступно.", None
            token = UIActionService(db).create(user, "clarify_target", {"message_id": str(item.message_id)}, ttl_seconds=3600)
            return f"Напишите: /clarify {token} <уточнение>", None
        if operation == "reply":
            message = db.get(Message, item.message_id) if item.message_id else None
            if not message or message.message_type != "email":
                return "Ответ доступен только для письма.", None
            return f"Напишите: /reply {message.id} <текст ответа>", None
        if operation == "create_task":
            title = (item.title or item.summary or "Задача из Inbox")[:500]
            values = {"title": title}
            if item.message_id:
                values["source_message_id"] = item.message_id
            try:
                task, undo = TaskService(db).create(user, values)
            except AppError as exc:
                return exc.message, None
            item.task_id = task.id
            item.status = "resolved"
            item.resolved_at = utcnow()
            db.commit()
            return outcome_message(db, user, {"state": "executed", "undo": undo})
        if operation == "snooze":
            InboxService(db).transition(user, item_id, "snoozed", utcnow() + timedelta(days=1))
            return "Элемент отложен на сутки.", None
        if operation in {"resolve", "ignore"}:
            InboxService(db).transition(user, item_id, "resolved" if operation == "resolve" else "ignored")
            return "Готово.", None
    if kind == "task_complete":
        task, undo = TaskService(db).change_status(user, uuid.UUID(payload["id"]), "completed")
        return outcome_message(db, user, {"state": "executed", "object_id": str(task.id), "undo": undo})
    if kind == "task_complete_prompt":
        task = TaskService(db).get(user, uuid.UUID(payload["id"]))
        return f"Отметить задачу «{task.title}» выполненной?", {"inline_keyboard": [[
            _button(db, user, "Да, выполнено", "task_complete", {"id": str(task.id)}),
            _button(db, user, "Не сейчас", "tasks_open", {"id": str(task.id)}),
        ]]}
    if kind == "task_edit_prompt":
        token = UIActionService(db).create(user, "task_edit_target", {"id": payload["id"]}, ttl_seconds=3600)
        text, markup = edit_task_draft(db, user, uuid.UUID(payload["id"]))
        return text + f"\nНазвание: /task_edit {token} <новое название>", markup
    if kind == "task_delete_prompt":
        task = TaskService(db).get(user, uuid.UUID(payload["id"]))
        return f"Удалить задачу «{task.title}»?", {"inline_keyboard": [[
            _button(db, user, "Удалить", "task_delete", {"id": str(task.id)}),
            _button(db, user, "Отмена", "cancel_ai", {}),
        ]]}
    if kind == "task_delete":
        task, undo = TaskService(db).delete(user, uuid.UUID(payload["id"]))
        return outcome_message(db, user, {"state": "executed", "object_id": str(task.id), "undo": undo})
    if kind == "project_archive":
        project = ProjectService(db).patch(user, uuid.UUID(payload["id"]), {"is_archived": True})
        return f"Проект «{project.name}» архивирован.", None
    if kind == "project_rename_prompt":
        project = ProjectService(db).get(user, uuid.UUID(payload["id"]))
        token = UIActionService(db).create(user, "project_rename_target", {"id": str(project.id)}, ttl_seconds=3600)
        return f"Чтобы переименовать «{project.name}», напишите: /project_rename {token} <новое название>", None
    if kind == "project_delete_prompt":
        project = ProjectService(db).get(user, uuid.UUID(payload["id"]))
        return (f"Удалить проект «{project.name}»? Задачи сохранятся в Inbox. "
                "Это действие нельзя отменить."), {"inline_keyboard": [[
            _button(db, user, "Да, удалить", "project_delete", {"id": str(project.id)}),
            _button(db, user, "Не удалять", "projects_open", {"id": str(project.id)}),
        ]]}
    if kind == "project_delete":
        project = ProjectService(db).get(user, uuid.UUID(payload["id"]))
        name = project.name
        ProjectService(db).delete(user, project.id)
        return f"Проект «{name}» удалён. Задачи сохранены в Inbox.", None
    if kind == "source_toggle":
        source = SourceService(db).get(user, uuid.UUID(payload["id"]))
        source = SourceService(db).patch(user, source.id, {
            "status": "paused" if source.status == "active" else "active",
        })
        return f"Источник «{source.name}»: {source.status}.", None
    if kind == "source_disconnect_prompt":
        source = SourceService(db).get(user, uuid.UUID(payload["id"]))
        return f"Отключить источник «{source.name}»? Сообщения и задачи сохранятся.", {"inline_keyboard": [[
            _button(db, user, "Отключить", "source_disconnect", {"id": str(source.id)}),
            _button(db, user, "Отмена", "cancel_ai", {}),
        ]]}
    if kind == "source_disconnect":
        source = SourceService(db).disconnect(user, uuid.UUID(payload["id"]))
        return f"Источник «{source.name}» отключён. Сохранённые данные остались.", None
    if kind == "source_projects_view":
        return source_projects_view(db, user, uuid.UUID(payload["id"]))
    if kind == "source_project_toggle":
        source_id = uuid.UUID(payload["id"])
        project_id = uuid.UUID(payload["project_id"])
        current = {project.id for project in SourceService(db).projects(user, source_id)}
        if project_id in current:
            current.remove(project_id)
        else:
            current.add(project_id)
        SourceService(db).replace_projects(user, source_id, list(current))
        return source_projects_view(db, user, source_id)
    if kind == "folder_refresh":
        source_id = uuid.UUID(payload["id"])
        try:
            EmailService(db).discover_folders(user, source_id)
        except AppError as exc:
            return f"Не удалось получить список папок: {exc.message}", None
        return folders_view(db, user, source_id)
    if kind == "folder_show":
        return folders_view(db, user, uuid.UUID(payload["id"]), int(payload.get("page", 0)))
    if kind == "folder_toggle":
        source_id = uuid.UUID(payload["id"])
        folders = {folder.external_folder_id: folder for folder in SourceService(db).folders(user, source_id)}
        folder = folders.get(payload["folder"])
        if not folder:
            return "Папка больше недоступна. Обновите список.", None
        try:
            SourceService(db).patch_folders(user, source_id, [{
                "external_folder_id": folder.external_folder_id,
                "is_selected": not folder.is_selected,
            }])
        except AppError as exc:
            return exc.message, None
        return folders_view(db, user, source_id, int(payload.get("page", 0)))
    if kind == "setting_toggle":
        field = payload["field"]
        if field not in {"morning_briefing_enabled", "evening_stats_enabled"}:
            return "Неизвестная настройка.", None
        settings = db.get(UserSettings, user.id)
        setattr(settings, field, not getattr(settings, field))
        db.commit()
        return section_view(db, user, "settings")
    if kind == "setting_times":
        return ("Утренняя сводка: /briefing_time 08:00\n"
                "Вечерняя статистика: /stats_time 20:00", None)
    if kind == "setting_projects":
        projects = db.scalars(select(Project).where(
            Project.user_id == user.id, Project.is_archived.is_(False),
        ).order_by(Project.name).limit(20)).all()
        rows = [[_button(db, user, "Inbox", "setting_project", {"id": None})]]
        rows.extend([_button(db, user, project.name[:40], "setting_project", {
            "id": str(project.id),
        })] for project in projects)
        return "Куда по умолчанию сохранять новые задачи?", {"inline_keyboard": rows}
    if kind == "setting_project":
        project_id = uuid.UUID(payload["id"]) if payload.get("id") else None
        if project_id:
            project = ProjectService(db).get(user, project_id)
            if project.is_archived:
                return "Архивный проект выбрать нельзя.", None
        settings = db.get(UserSettings, user.id)
        settings.default_project_id = project_id
        db.commit()
        return section_view(db, user, "settings")
    if kind == "task_snooze":
        task_id = uuid.UUID(payload["id"])
        task = TaskService(db).get(user, task_id)
        base = task.due_at or utcnow()
        new_due = base + timedelta(days=1)
        task, undo = TaskService(db).patch(user, task_id, {"due_at": new_due})
        return outcome_message(db, user, {"state": "executed", "object_id": str(task.id), "undo": undo})
    if kind == "task_add_calendar":
        task_id = uuid.UUID(payload["task_id"])
        task = TaskService(db).get(user, task_id)
        connection = db.scalar(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user.id, CalendarConnection.status == "active"
            )
        )
        if not connection:
            return "Подключение Google Calendar не найдено.", None
        start = task.due_at or utcnow()
        end = start + timedelta(hours=1)
        event = CalendarService(db).create(user, {
            "connection_id": connection.id,
            "title": task.title,
            "description": task.description,
            "start_at": start,
            "end_at": end,
            "task_id": task.id,
        })
        return f"📅 Событие «{event.title}» добавлено в Google Calendar.", None
    if kind == "source_setting_toggle":
        source_id = uuid.UUID(payload["id"])
        source = SourceService(db).get(user, source_id)
        field = payload["field"]
        if field in {"analysis_text", "analysis_voice", "save_attachments"}:
            new_val = not getattr(source, field)
            SourceService(db).patch(user, source_id, {field: new_val})
        return object_view(db, user, "sources", source_id)
    if kind == "waiting_open":
        item = WaitingService(db).get(user, uuid.UUID(payload["id"]))
        text = f"⏳ Ожидание: {item.title}\nСтатус: {item.status}"
        rows = []
        if item.status in {"active", "pending"}:
            rows.append([
                _button(db, user, "Завершено", "waiting_complete", {"id": str(item.id)}),
                _button(db, user, "Отменить", "waiting_cancel", {"id": str(item.id)}),
            ])
        return text, {"inline_keyboard": rows} if rows else None
    if kind == "waiting_complete":
        WaitingService(db).transition(user, uuid.UUID(payload["id"]), "completed")
        return "Ожидание отмечено завершённым.", None
    if kind == "waiting_cancel":
        WaitingService(db).transition(user, uuid.UUID(payload["id"]), "cancelled")
        return "Ожидание отменено.", None
    if kind == "reminder_cancel":
        ReminderService(db).cancel(user, uuid.UUID(payload["id"]))
        return "Напоминание отменено.", None
    if kind == "cancel_ai":
        return "Действие отменено.", None
    if kind == "edit_ai":
        return "Отправьте уточнённое сообщение с изменениями.", None
    if kind == "setting_timezone":
        return ("Укажите часовой пояс: /timezone Europe/Moscow\n"
                "Или укажите ваш город/регион, например /timezone Asia/Yekaterinburg"), None
    if kind == "source_connect_menu":
        from app.bot.telegram import email_connect_menu
        return "Подключить почту или календарь:", email_connect_menu(db, user)
    if kind == "task_disambiguate_edit":
        task, undo = TaskService(db).patch(user, uuid.UUID(payload["id"]), {"title": payload["title"]})
        return outcome_message(db, user, {"state": "executed", "object_id": str(task.id), "undo": undo})
    if kind == "project_disambiguate_rename":
        project = ProjectService(db).patch(user, uuid.UUID(payload["id"]), {"name": payload["name"]})
        return f"Проект переименован: {project.name}", None
    return "Действие недоступно.", None
