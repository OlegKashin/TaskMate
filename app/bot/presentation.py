from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import CalendarEvent, InboxItem, Project, Source, Task, User, UserSettings
from app.services.domain import UIActionService, utcnow


def outcome_message(db: Session, user: User, outcome: dict) -> tuple[str, dict | None]:
    state = outcome.get("state")
    if state == "email_draft":
        token = outcome["token"]
        edit = UIActionService(db).create(user, "edit_email", {
            "send_token": token, "message_id": outcome["message_id"]})
        cancel = UIActionService(db).create(user, "cancel_email", {"send_token": token})
        return (f"Черновик ответа:\n{outcome['draft_text']}", {"inline_keyboard": [[
            {"text": "Отправить", "callback_data": token},
            {"text": "Изменить текст", "callback_data": edit},
            {"text": "Отмена", "callback_data": cancel},
        ]]})
    if state == "calendar_not_connected":
        connect = UIActionService(db).create(user, "connect_provider", {"provider": "google"})
        cancel = UIActionService(db).create(user, "cancel_ai", {})
        return "Чтобы создавать события, подключите Google Calendar.", {"inline_keyboard": [[
            {"text": "Подключить", "callback_data": connect},
            {"text": "Отмена", "callback_data": cancel},
        ]]}
    if state == "project_choice":
        candidates = outcome.get("candidates", [])
        rows = [[{
            "text": project["name"][:40],
            "callback_data": UIActionService(db).create(user, "select_project", {
                "project_id": project["id"], "result": outcome["result"],
                "message_id": outcome["message_id"],
            }),
        }] for project in candidates]
        if not outcome.get("all_projects"):
            rows.append([{
                "text": "Другой",
                "callback_data": UIActionService(db).create(user, "project_choice_other", {
                    "result": outcome["result"], "message_id": outcome["message_id"],
                }),
            }])
        rows.append([{
            "text": "Inbox",
            "callback_data": UIActionService(db).create(user, "select_project", {
                "project_id": None, "result": outcome["result"],
                "message_id": outcome["message_id"],
            }),
        }])
        return "К какому проекту отнести задачу?", {"inline_keyboard": rows}
    if state == "proposal":
        entities = outcome.get("entities", {})
        title = entities.get("title") or entities.get("target") or outcome["intent"]
        action_names = {
            "create_task": "создать задачу", "edit_task": "изменить задачу",
            "delete_task": "удалить задачу", "change_task_status": "изменить статус задачи",
            "create_event": "создать событие", "project_action": "действие с проектом",
            "source_action": "действие с источником", "reminder": "создать напоминание",
            "create_waiting_for": "добавить ожидание",
        }
        text = f"Предлагаю: {action_names.get(outcome['intent'], outcome['intent'])}\n{title}"
        if outcome["intent"] == "project_action" and entities.get("action") == "delete":
            text += "\n⚠️ Проект будет удалён без возможности Undo. Задачи сохранятся без проекта."
        if outcome["intent"] == "source_action" and entities.get("action") == "disconnect":
            text += "\n⚠️ Источник будет отключён, новые сообщения перестанут обрабатываться."
        if outcome["confidence"] == "medium":
            text += f"\nНужно уточнить: {outcome.get('reason', '')}"
        buttons = [{"text": "Подтвердить", "callback_data": outcome["token"]}]
        if outcome["confidence"] == "medium":
            edit = UIActionService(db).create(user, "edit_ai", {}, ttl_seconds=900)
            buttons.append({"text": "Изменить", "callback_data": edit})
        cancel = UIActionService(db).create(user, "cancel_ai", {}, ttl_seconds=900)
        buttons.append({"text": "Отмена", "callback_data": cancel})
        return text, {"inline_keyboard": [buttons]}
    if state == "executed":
        text = "✅ Действие выполнено."
        buttons = []
        if outcome.get("suggest_calendar") and outcome.get("object_id"):
            text += "\n📅 Добавить в Google Calendar?"
            add_cal = UIActionService(db).create(
                user, "task_add_calendar", {"task_id": outcome["object_id"]}
            )
            buttons.append({"text": "Да, добавить", "callback_data": add_cal})
        if outcome.get("undo"):
            buttons.append({"text": "Отменить", "callback_data": outcome["undo"]})
        return text, {"inline_keyboard": [buttons]} if buttons else None
    if state == "search_results":
        tasks = outcome.get("tasks", [])
        if not tasks:
            return f"Не нашёл задач по запросу «{outcome.get('query', '')}».", None
        rows = [[{
            "text": task["title"][:40],
            "callback_data": UIActionService(db).create(user, "tasks_open", {"id": task["id"]}),
        }] for task in tasks]
        return f"🔎 Нашёл задач: {len(tasks)}", {"inline_keyboard": rows}
    if state == "clarification":
        lines = [outcome.get("reason") or "Уточните запрос, пожалуйста."]
        candidates = outcome.get("candidates", [])
        lines.extend(f"{index}. {item['title']}" for index, item in enumerate(candidates, 1))
        if candidates and outcome.get("result"):
            rows = [[{
                "text": item["title"][:40],
                "callback_data": UIActionService(db).create(user, "select_task", {
                    "task_id": item["id"], "result": outcome["result"],
                    "message_id": outcome.get("message_id"),
                }),
            }] for item in candidates]
            return "\n".join(lines), {"inline_keyboard": rows}
        return "\n".join(lines), None
    return outcome.get("reason") or "Запрос обработан.", None


def local_time(value: datetime, zone: ZoneInfo) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(zone)


def section_message(db: Session, user: User, section: str) -> str:
    try:
        zone = ZoneInfo(user.timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local_now = datetime.now(zone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = day_start.astimezone(UTC), (day_start + timedelta(days=1)).astimezone(UTC)
    if section in {"today", "schedule"}:
        events = db.scalars(select(CalendarEvent).where(
            CalendarEvent.user_id == user.id, CalendarEvent.status != "cancelled",
            CalendarEvent.start_at >= start, CalendarEvent.start_at < end,
        ).order_by(CalendarEvent.start_at)).all()
        if section == "schedule":
            return "📅 Расписание\n" + ("\n".join(
                f"{local_time(event.start_at, zone):%H:%M} — {event.title}" for event in events
            ) or "Событий на сегодня нет.")
        tasks = db.scalars(select(Task).where(
            Task.user_id == user.id, Task.deleted_at.is_(None),
            Task.due_at >= start, Task.due_at < end,
        ).order_by(Task.due_at)).all()
        overdue = db.scalar(select(func.count()).select_from(Task).where(
            Task.user_id == user.id, Task.deleted_at.is_(None),
            Task.status.not_in(["completed", "cancelled"]), Task.due_at < start,
        )) or 0
        lines = ["☀️ План на сегодня"]
        lines.extend(f"{local_time(event.start_at, zone):%H:%M} — {event.title}" for event in events)
        for task in tasks:
            icon = "🔴 " if task.priority == "high" else ""
            lines.append(f"{icon}{local_time(task.due_at, zone):%H:%M} — {task.title}")
        if not events and not tasks:
            lines.append("На сегодня задач и событий нет.")
        if overdue:
            lines.append(f"Просрочено: {overdue}")
        return "\n".join(lines)
    if section == "stats":
        created = db.scalar(select(func.count()).select_from(Task).where(
            Task.user_id == user.id, Task.created_at >= start, Task.created_at < end,
        )) or 0
        completed = db.scalar(select(func.count()).select_from(Task).where(
            Task.user_id == user.id, Task.completed_at >= start, Task.completed_at < end,
        )) or 0
        tomorrow_start = end
        tomorrow_end = (day_start + timedelta(days=2)).astimezone(UTC)
        tomorrow = db.scalar(select(func.count()).select_from(Task).where(
            Task.user_id == user.id, Task.deleted_at.is_(None),
            Task.status.not_in(["completed", "cancelled"]),
            Task.due_at >= tomorrow_start, Task.due_at < tomorrow_end,
        )) or 0
        return f"🌙 Итоги дня\nСоздано задач: {created}\nВыполнено: {completed}\nЗавтра: {tomorrow} задач"
    if section == "settings":
        settings = db.get(UserSettings, user.id)
        return (f"⚙️ Настройки и интеграции\nЧасовой пояс: {user.timezone}\n"
                f"Утренняя сводка: {'вкл' if settings.morning_briefing_enabled else 'выкл'}, {settings.morning_briefing_time:%H:%M}\n"
                f"Вечерняя статистика: {'вкл' if settings.evening_stats_enabled else 'выкл'}, {settings.evening_stats_time:%H:%M}")
    model = {"tasks": Task, "projects": Project, "inbox": InboxItem, "sources": Source}.get(section)
    if model:
        conditions = [model.user_id == user.id]
        if model is Task:
            conditions.append(Task.deleted_at.is_(None))
        if model is InboxItem:
            conditions.append(
                or_(
                    InboxItem.status.in_(["new", "proposed"]),
                    and_(InboxItem.status == "snoozed", or_(InboxItem.snoozed_until.is_(None), InboxItem.snoozed_until <= utcnow())),
                )
            )
        items = db.scalars(select(model).where(*conditions).limit(10)).all()
        heading = {
            "tasks": "📋 Задачи и проекты",
            "projects": "📁 Проекты",
            "inbox": "📥 Предложения AI",
            "sources": "🔗 Источники",
        }[section]
        return f"{heading}\n" + ("\n".join(
            f"• {getattr(item, 'title', None) or getattr(item, 'name', None) or getattr(item, 'summary', None) or item.id}" for item in items
        ) or "Пока пусто.")
    return "Раздел не найден."
