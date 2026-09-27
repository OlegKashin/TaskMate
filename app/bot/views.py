"""Small Telegram cards backed by short-lived, owner-bound callback actions."""

import uuid

from sqlalchemy import and_, or_, select

from app.ai.service import MUTATING_INTENTS
from app.bot.presentation import local_time, section_message
from app.models.entities import (
    AIProcessingJob,
    CalendarEvent,
    InboxItem,
    Message,
    Project,
    Source,
    Task,
    UserSettings,
)
from app.schemas.domain import AIResult
from app.services.domain import UIActionService, utcnow


def _button(db, user, label: str, action: str, payload: dict) -> dict:
    return {"text": label, "callback_data": UIActionService(db).create(user, action, payload)}


def section_view(db, user, section: str) -> tuple[str, dict | None]:
    text = section_message(db, user, section)
    if section == "settings":
        settings = db.get(UserSettings, user.id)
        rows = [
            [_button(db, user, "Изменить часовой пояс", "setting_timezone", {})],
            [_button(db, user, "Утренняя сводка вкл/выкл", "setting_toggle", {"field": "morning_briefing_enabled"})],
            [_button(db, user, "Вечерняя статистика вкл/выкл", "setting_toggle", {"field": "evening_stats_enabled"})],
            [_button(db, user, "Время сводок", "setting_times", {})],
            [_button(db, user, "Проект по умолчанию", "setting_projects", {})],
        ]
        default_project = db.get(Project, settings.default_project_id) if settings.default_project_id else None
        text += (f"\nПроект по умолчанию: {default_project.name if default_project else 'Inbox'}"
                 "\nЧасовой пояс: /timezone Europe/Moscow")
        return text, {"inline_keyboard": rows}
    model = {"tasks": Task, "projects": Project, "inbox": InboxItem,
             "sources": Source, "schedule": CalendarEvent}.get(section)
    if model is None:
        return text, None
    query = select(model).where(model.user_id == user.id)
    if model is Task:
        query = query.where(Task.deleted_at.is_(None))
    if model is InboxItem:
        query = query.where(
            or_(
                InboxItem.status.in_(["new", "proposed"]),
                and_(InboxItem.status == "snoozed", or_(InboxItem.snoozed_until.is_(None), InboxItem.snoozed_until <= utcnow())),
            )
        )
    if model is CalendarEvent:
        query = query.where(CalendarEvent.status != "cancelled").order_by(CalendarEvent.start_at)
    else:
        query = query.order_by(model.created_at.desc())
    items = db.scalars(query.limit(10)).all()
    rows = [[_button(db, user, str(getattr(item, "title", None) or getattr(item, "name", None) or item.id)[:40],
                     f"{section}_open", {"id": str(item.id)})] for item in items]
    if section == "tasks":
        rows.insert(0, [_button(db, user, "Создать задачу", "task_new_prompt", {})])
    if section == "projects":
        rows.insert(0, [_button(db, user, "Создать проект", "project_new_prompt", {})])
    if section == "sources":
        rows.insert(0, [_button(db, user, "Подключить источник", "source_connect_menu", {})])
    return text, {"inline_keyboard": rows} if rows else None


def inbox_view(db, user, item_id: uuid.UUID) -> tuple[str, dict]:
    item = db.get(InboxItem, item_id)
    if not item or item.user_id != user.id:
        return "Элемент Inbox не найден.", {"inline_keyboard": []}
    message = db.get(Message, item.message_id) if item.message_id else None
    text = f"📥 {item.title or item.item_type}\n{item.summary or ''}\nСтатус: {item.status}"
    rows = []
    if message:
        job = db.scalar(select(AIProcessingJob).where(
            AIProcessingJob.message_id == message.id,
            AIProcessingJob.status == "completed",
        ).order_by(AIProcessingJob.created_at.desc()))
        if job and job.result and job.result.get("ai"):
            result = AIResult.model_validate(job.result["ai"])
            if job.result.get("outcome", {}).get("state") == "calendar_not_connected":
                text += "\nДля события нужен Google Calendar."
                rows.append([_button(db, user, "Подключить Calendar", "connect_provider", {
                    "provider": "google",
                })])
            elif result.intent in MUTATING_INTENTS and result.confidence >= 0.60:
                rows.append([_button(db, user, "Подтвердить предложение", "confirm_ai", {
                    "result": result.model_dump(mode="json"), "message_id": str(message.id),
                })])
            elif result.confidence < 0.60:
                text += f"\nУточните: {result.reason}"
                rows.append([_button(db, user, "Уточнить", "inbox_action", {
                    "id": str(item.id), "operation": "clarify",
                })])
    actions = [
        _button(db, user, "Создать задачу", "inbox_action", {"id": str(item.id), "operation": "create_task"}),
        _button(db, user, "Отложить на день", "inbox_action", {"id": str(item.id), "operation": "snooze"}),
    ]
    rows.append(actions)
    if message and message.message_type == "email":
        rows.append([_button(db, user, "Ответить", "inbox_action", {"id": str(item.id), "operation": "reply"})])
    rows.append([
        _button(db, user, "Обработано", "inbox_action", {"id": str(item.id), "operation": "resolve"}),
        _button(db, user, "Игнорировать", "inbox_action", {"id": str(item.id), "operation": "ignore"}),
    ])
    return text, {"inline_keyboard": rows}


def object_view(db, user, section: str, object_id: uuid.UUID) -> tuple[str, dict | None]:
    model = {"tasks": Task, "projects": Project, "sources": Source,
             "schedule": CalendarEvent}.get(section)
    item = db.get(model, object_id) if model else None
    if not item or item.user_id != user.id:
        return "Объект не найден.", None
    if section == "schedule":
        text = f"📅 {item.title}\nСтатус: {item.status}"
        rows = []
        if item.status == "pending" and item.sync_attempts >= 4:
            rows.append([_button(db, user, "Повторить синхронизацию", "calendar_retry", {
                "id": str(item.id),
            })])
        if item.status != "cancelled":
            rows.append([_button(db, user, "Отменить событие…", "calendar_cancel_prompt", {
                "id": str(item.id),
            })])
    elif section == "tasks":
        status_labels = {
            "new": "🆕 Новая",
            "in_progress": "🔄 В работе",
            "completed": "✅ Выполнена",
            "cancelled": "🚫 Отменена",
        }
        priority_labels = {
            "low": "🟢 Низкий",
            "normal": "🟡 Обычный",
            "high": "🔴 Высокий",
        }
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(user.timezone)
        except Exception:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("UTC")
        if item.due_at:
            due_dt = local_time(item.due_at, zone)
            due_str = f"{due_dt:%d.%m.%Y %H:%M}" if (due_dt.hour or due_dt.minute) else f"{due_dt:%d.%m.%Y}"
        else:
            due_str = "Без срока"
        project = db.get(Project, item.project_id) if item.project_id else None
        project_name = project.name if project else "Без проекта"
        source = None
        if item.source_message_id:
            msg = db.get(Message, item.source_message_id)
            if msg and msg.source_id:
                source = db.get(Source, msg.source_id)
        source_name = source.name if source else "Ручной ввод"
        desc = item.description if item.description else "отсутствует"
        text = (
            f"✅ {item.title}\n"
            f"Статус: {status_labels.get(item.status, item.status)}\n"
            f"Срок: {due_str}\n"
            f"Приоритет: {priority_labels.get(item.priority, item.priority)}\n"
            f"Проект: {project_name}\n"
            f"Источник: {source_name}\n"
            f"Описание: {desc}"
        )
        rows = []
        if item.status != "completed":
            rows.append([_button(db, user, "Выполнить", "task_complete_prompt", {"id": str(item.id)})])
        rows.append([_button(db, user, "Изменить", "task_edit_prompt", {"id": str(item.id)})])
        rows.append([_button(db, user, "Отложить на 1 день", "task_snooze", {"id": str(item.id)})])
        rows.append([_button(db, user, "Удалить…", "task_delete_prompt", {"id": str(item.id)})])
    elif section == "projects":
        text = f"📁 {item.name}\n{'Архив' if item.is_archived else 'Активен'}"
        rows = [[_button(db, user, "Переименовать", "project_rename_prompt", {"id": str(item.id)})],
                [_button(db, user, "Архивировать", "project_archive", {"id": str(item.id)})],
                [_button(db, user, "Удалить…", "project_delete_prompt", {"id": str(item.id)})]]
    else:
        text = (
            f"🔗 {item.name}\n"
            f"Тип: {item.type}\n"
            f"Статус: {item.status}\n"
            f"Анализ текста: {'вкл' if item.analysis_text else 'выкл'}\n"
            f"Анализ голосовых: {'вкл' if item.analysis_voice else 'выкл'}\n"
            f"Сохранять вложения: {'вкл' if item.save_attachments else 'выкл'}"
        )
        rows = [
            [_button(db, user, "Проекты источника", "source_projects_view", {"id": str(item.id)})],
            [
                _button(db, user, f"Текст: {'вкл' if item.analysis_text else 'выкл'}", "source_setting_toggle", {"id": str(item.id), "field": "analysis_text"}),
                _button(db, user, f"Голос: {'вкл' if item.analysis_voice else 'выкл'}", "source_setting_toggle", {"id": str(item.id), "field": "analysis_voice"}),
                _button(db, user, f"Вложения: {'вкл' if item.save_attachments else 'выкл'}", "source_setting_toggle", {"id": str(item.id), "field": "save_attachments"}),
            ],
        ]
        if item.type in {"gmail", "yandex", "mailru", "imap"} and item.status == "active":
            rows.append([_button(db, user, "Выбрать папки", "folder_refresh", {"id": str(item.id)})])
        if item.status in {"active", "paused"}:
            rows.append([_button(db, user, "Пауза/возобновить", "source_toggle", {"id": str(item.id)})])
        if item.status != "disconnected":
            rows.append([_button(db, user, "Отключить…", "source_disconnect_prompt", {"id": str(item.id)})])
    return text, {"inline_keyboard": rows} if rows else None


def source_projects_view(db, user, source_id: uuid.UUID) -> tuple[str, dict]:
    from app.services.domain import SourceService

    linked = {project.id for project in SourceService(db).projects(user, source_id)}
    projects = db.scalars(select(Project).where(
        Project.user_id == user.id, Project.is_archived.is_(False),
    ).order_by(Project.name).limit(50)).all()
    rows = [[_button(db, user, f"{'☑' if project.id in linked else '☐'} {project.name}"[:60],
                     "source_project_toggle", {"id": str(source_id),
                                               "project_id": str(project.id)})]
            for project in projects]
    rows.append([_button(db, user, "Готово", "sources_open", {"id": str(source_id)})])
    return "Какие проекты связаны с источником?", {"inline_keyboard": rows}


def folders_view(db, user, source_id: uuid.UUID, page: int = 0) -> tuple[str, dict]:
    from app.services.domain import SourceService

    folders = sorted(SourceService(db).folders(user, source_id), key=lambda item: item.name.lower())
    page = max(0, min(page, max(0, (len(folders) - 1) // 8)))
    rows = [[_button(db, user, f"{'☑' if item.is_selected else '☐'} {item.name}"[:60],
                     "folder_toggle", {"id": str(source_id), "folder": item.external_folder_id, "page": page})]
            for item in folders[page * 8:(page + 1) * 8]]
    pages = []
    if page:
        pages.append(_button(db, user, "←", "folder_show", {"id": str(source_id), "page": page - 1}))
    if (page + 1) * 8 < len(folders):
        pages.append(_button(db, user, "→", "folder_show", {"id": str(source_id), "page": page + 1}))
    if pages:
        rows.append(pages)
    rows.append([_button(db, user, "Обновить список", "folder_refresh", {"id": str(source_id)})])
    return "Какие папки читать? Новые папки начнут отслеживаться с момента выбора.", {"inline_keyboard": rows}
