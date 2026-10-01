import uuid
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.service import AIActionService
from app.api.deps import DB
from app.bot.actions import ACTIONS, handle_action
from app.bot.manual import new_task_draft, set_task_due
from app.bot.presentation import outcome_message, section_message
from app.bot.views import section_view
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import issue_user_token
from app.integrations.telegram.client import TelegramClient, main_menu
from app.models.entities import (
    AIProcessingJob,
    InboxItem,
    Message,
    Project,
    Source,
    Task,
    TelegramUpdate,
    UIAction,
    User,
    UserSettings,
)
from app.schemas.domain import AIResult
from app.services.analysis import queue_source_analysis
from app.services.domain import (
    ProjectService,
    SourceService,
    TaskService,
    UIActionService,
    UserService,
    utcnow,
)
from app.services.email import EmailService
from app.services.oauth import OAuthService

router = APIRouter()


def email_connect_menu(db, user):
    actions = UIActionService(db)
    labels = (
        ("gmail", "Gmail"),
        ("yandex", "Яндекс.Почта"),
        ("mailru", "Mail.ru"),
        ("imap", "Другой IMAP"),
        ("google", "Google Calendar"),
    )
    return {
        "inline_keyboard": [
            [
                {
                    "text": label,
                    "callback_data": actions.create(
                        user, "connect_provider", {"provider": provider}
                    ),
                }
                for provider, label in labels[:2]
            ],
            [
                {
                    "text": label,
                    "callback_data": actions.create(
                        user, "connect_provider", {"provider": provider}
                    ),
                }
                for provider, label in labels[2:4]
            ],
            [
                {
                    "text": labels[4][1],
                    "callback_data": actions.create(user, "connect_provider", {"provider": labels[4][0]}),
                }
            ],
        ]
    }


def send_oauth_link(db, user, chat_id: int, provider: str):
    service = OAuthService(db)
    state = service.create_state(user, provider)
    url = service.authorize_url(provider, state.state_token)
    TelegramClient().send_message(
        chat_id,
        "Откройте ссылку для подключения аккаунта:",
        {"inline_keyboard": [[{"text": "Подключить", "url": url}]]},
    )
    return state


def resolve_target_id(db, user, raw_id: str, model_cls) -> uuid.UUID:
    raw = raw_id.strip()
    try:
        return uuid.UUID(raw)
    except ValueError:
        pass
    if raw.startswith("a:") or len(raw) <= 12:
        try:
            from app.services.domain import from_base36
            action_id = from_base36(raw.removeprefix("a:"))
            action = db.get(UIAction, action_id)
            if action and action.user_id == user.id:
                target_str = (
                    action.payload.get("id")
                    or action.payload.get("task_id")
                    or action.payload.get("project_id")
                    or action.payload.get("message_id")
                )
                if target_str:
                    return uuid.UUID(target_str)
        except Exception:
            pass
    from sqlalchemy import String, cast
    match = db.scalar(
        select(model_cls.id).where(
            model_cls.user_id == user.id,
            cast(model_cls.id, String).like(f"{raw}%"),
        )
    )
    if match:
        return match
    raise AppError("VALIDATION_ERROR", f"Invalid {model_cls.__name__} ID", 422)


@router.post("/webhooks/telegram")
def telegram_webhook(
    payload: dict,
    db: DB,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    if secret != get_settings().telegram_webhook_secret:
        raise AppError("AUTH_ERROR", "Invalid Telegram webhook secret", 401)
    return process_telegram_update(db, payload)


def process_telegram_update(db: Session, payload: dict) -> dict:
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise AppError("VALIDATION_ERROR", "update_id is required", 422)
    if db.get(TelegramUpdate, update_id):
        return {"ok": True, "duplicate": True}
    db.add(TelegramUpdate(update_id=update_id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"ok": True, "duplicate": True}
    callback = payload.get("callback_query")
    if callback:
        tg_id = callback.get("from", {}).get("id")
        if not isinstance(tg_id, int):
            raise AppError("VALIDATION_ERROR", "callback sender is required", 422)
        user = UserService(db).get_or_create(tg_id)
        client = TelegramClient()
        try:
            action = UIActionService(db).consume(user, callback.get("data", ""))
        except AppError:
            client.answer_callback(callback.get("id"), "Кнопка уже использована или устарела")
            db.rollback()
            return {"ok": True, "expired": True}
        callback_message = callback.get("message", {})
        chat_id = callback_message.get("chat", {}).get("id", tg_id)
        message_id = callback_message.get("message_id")
        if callback_message.get("chat", {}).get("type") != "private" and (
            action.action in ACTIONS or action.action in {
                "navigate", "select_task", "select_project", "project_choice_other",
                "send_email", "retry_email", "edit_email",
            }
        ):
            client.answer_callback(callback.get("id"), "Откройте этот раздел в личном чате с ботом")
            db.commit()
            return {"ok": True, "ignored": True, "reason": "private_action"}
        if action.action in ACTIONS:
            text, markup = handle_action(db, user, action)
            if message_id:
                client.edit_message(chat_id, message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": action.action}
        if action.action in {"retry_job", "leave_job"}:
            job = db.get(AIProcessingJob, uuid.UUID(action.payload["id"]))
            if not job or job.user_id != user.id:
                response = "Сообщение для повторной обработки не найдено."
            elif action.action == "leave_job":
                response = "Сообщение сохранено. Позже используйте /analyze."
            else:
                job.status = "queued"
                job.error_code = None
                original = db.get(Message, job.message_id) if job.message_id else None
                if original:
                    original.processing_status = "queued"
                db.commit()
                from app.workers.tasks import process_message

                try:
                    process_message.delay(str(job.id))
                    response = "Повторная обработка запущена."
                except Exception:
                    response = "Очередь временно недоступна. Сообщение сохранено."
            if message_id:
                client.edit_message(chat_id, message_id, response)
            else:
                client.send_message(chat_id, response)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": action.action}
        if action.action == "project_choice_other":
            projects = db.scalars(select(Project).where(
                Project.user_id == user.id, Project.is_archived.is_(False),
            ).order_by(Project.name).limit(50)).all()
            outcome = {
                "state": "project_choice", "all_projects": True,
                "candidates": [{"id": str(project.id), "name": project.name}
                               for project in projects],
                "result": action.payload["result"],
                "message_id": action.payload["message_id"],
            }
            text, markup = outcome_message(db, user, outcome)
            if message_id:
                client.edit_message(chat_id, message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": "project_choice_other"}
        if action.action == "select_project":
            project_id = action.payload.get("project_id")
            if project_id:
                project = ProjectService(db).get(user, uuid.UUID(project_id))
                if project.is_archived:
                    raise AppError("PROJECT_ARCHIVED", "Project is archived", 422)
            message = db.get(Message, uuid.UUID(action.payload["message_id"]))
            if not message or message.user_id != user.id:
                raise AppError("MESSAGE_NOT_FOUND", "Message not found", 404)
            result = AIResult.model_validate(action.payload["result"])
            result = result.model_copy(update={
                "entities": {**result.entities, "project_id": project_id},
            })
            outcome = AIActionService(db).apply(user, result, message)
            text, markup = outcome_message(db, user, outcome)
            if message_id:
                client.edit_message(chat_id, message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": "select_project"}
        if action.action == "select_task":
            selected = TaskService(db).get(user, uuid.UUID(action.payload["task_id"]))
            result = AIResult.model_validate(action.payload["result"])
            result = result.model_copy(update={
                "entities": {**result.entities, "target": {"task_id": str(selected.id)}},
            })
            message = db.get(Message, uuid.UUID(action.payload["message_id"])) if action.payload.get("message_id") else None
            if message and message.user_id != user.id:
                raise AppError("MESSAGE_NOT_FOUND", "Message not found", 404)
            outcome = AIActionService(db).apply(user, result, message)
            text, markup = outcome_message(db, user, outcome)
            if message_id:
                client.edit_message(chat_id, message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": "select_task"}
        if action.action == "confirm_ai":
            message = (
                db.get(Message, uuid.UUID(action.payload["message_id"]))
                if action.payload.get("message_id")
                else None
            )
            if message and message.user_id != user.id:
                raise AppError("MESSAGE_NOT_FOUND", "Message not found", 404)
            outcome = AIActionService(db).execute(
                user, AIResult.model_validate(action.payload["result"]), message
            )
            if message and outcome.get("state") == "executed":
                for item in db.scalars(select(InboxItem).where(
                    InboxItem.user_id == user.id, InboxItem.message_id == message.id,
                    InboxItem.status == "proposed",
                )):
                    item.status = "resolved"
                    item.resolved_at = utcnow()
            text, markup = outcome_message(db, user, outcome)
            if message_id:
                client.edit_message(chat_id, message_id, text, markup)
            else:
                client.send_message(chat_id, text, markup)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "outcome": outcome}
        if action.action == "undo_task":
            task = TaskService(db).undo(user, callback.get("data", ""), consumed_action=action)
            if message_id:
                client.edit_message(chat_id, message_id, f"↩️ Действие отменено: {task.title}")
            client.answer_callback(callback.get("id"))
            return {"ok": True, "undone": str(task.id)}
        if action.action == "navigate":
            section = action.payload["section"]
            if section == "menu":
                action_service = UIActionService(db)
                tokens = {
                    sec: action_service.create(
                        user, "navigate", {"section": sec}, ttl_seconds=86400
                    )
                    for sec in (
                        "today",
                        "tasks",
                        "projects",
                        "inbox",
                        "sources",
                        "schedule",
                        "settings",
                    )
                }
                client.send_message(
                    chat_id, "TaskMate AI готов. Выберите раздел:", main_menu(tokens)
                )
                client.answer_callback(callback.get("id"))
                db.commit()
                return {"ok": True, "section": "menu"}
            section_text, section_markup = section_view(db, user, section)
            if section == "sources" and callback_message.get("chat", {}).get("type") == "private":
                connect_rows = email_connect_menu(db, user)["inline_keyboard"]
                section_markup = {"inline_keyboard": (section_markup or {"inline_keyboard": []})["inline_keyboard"] + connect_rows}
            client.send_message(
                chat_id,
                section_text,
                section_markup,
            )
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "section": section}
        if action.action == "connect_provider":
            if callback_message.get("chat", {}).get("type") != "private":
                client.answer_callback(callback.get("id"), "Откройте меню в личном чате с ботом")
                db.commit()
                return {"ok": True, "ignored": True}
            provider = action.payload["provider"]
            if provider in {"gmail", "yandex", "google"}:
                try:
                    send_oauth_link(db, user, chat_id, provider)
                except AppError as exc:
                    client.send_message(chat_id, f"Не удалось начать подключение: {exc.message}")
            elif provider == "mailru":
                client.send_message(chat_id, "Укажите адрес ящика: /connect_mailru <email>")
            else:
                client.send_message(
                    chat_id,
                    "Для другого IMAP создайте источник через API и укажите app password в /sources/{id}/imap-credentials. Получить API-токен: /api_token",
                )
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "provider": provider}
        if action.action == "send_email":
            # The callback action is already claimed by UIActionService.consume.
            db.commit()
            try:
                sent = EmailService(db).send_consumed_reply(user, action)
            except AppError as exc:
                retry = UIActionService(db).create(user, "retry_email", {
                    "message_id": action.payload["message_id"],
                    "draft_text": action.payload["draft_text"],
                })
                edit = UIActionService(db).create(user, "edit_email", {
                    "message_id": action.payload["message_id"],
                    "send_token": callback.get("data", ""),
                })
                client.send_message(
                    chat_id,
                    (
                        "Не удалось подтвердить отправку. Черновик сохранён. "
                        "Перед повтором проверьте папку «Отправленные»: письмо могло уйти до обрыва связи. "
                        f"Черновик: {action.payload['draft_text'][:2000]}\n"
                        f"Причина: {exc.message}"
                    ),
                    {"inline_keyboard": [[
                        {"text": "Повторить после проверки", "callback_data": retry},
                        {"text": "Изменить", "callback_data": edit},
                    ]]},
                )
                client.answer_callback(callback.get("id"))
                return {"ok": True, "sent": False, "error": exc.code}
            client.send_message(chat_id, "Ответ отправлен.")
            target = uuid.UUID(action.payload["message_id"])
            for item in db.scalars(select(InboxItem).where(
                InboxItem.user_id == user.id, InboxItem.message_id == target,
                InboxItem.item_type == "reply_required", InboxItem.status == "proposed",
            )):
                item.status = "resolved"
                item.resolved_at = utcnow()
            db.commit()
            client.answer_callback(callback.get("id"))
            return {"ok": True, "sent": sent}
        if action.action == "retry_email":
            token = EmailService(db).draft_for_message(
                user, uuid.UUID(action.payload["message_id"]), action.payload["draft_text"]
            )
            text, markup = outcome_message(db, user, {
                "state": "email_draft", "token": token,
                "message_id": action.payload["message_id"],
                "draft_text": action.payload["draft_text"],
            })
            client.send_message(chat_id, "После проверки «Отправленных» подтвердите повторную отправку.\n" + text, markup)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "draft": True}
        if action.action in {"edit_email", "cancel_email"}:
            try:
                UIActionService(db).consume(user, action.payload["send_token"], "send_email")
            except AppError:
                pass
            response = (
                f"Напишите новый ответ командой /reply {action.payload['message_id']} <текст>"
                if action.action == "edit_email"
                else "Отправка ответа отменена."
            )
            client.send_message(chat_id, response)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": action.action}
        if action.action in {"cancel_ai", "edit_ai"}:
            response = (
                "Действие отменено."
                if action.action == "cancel_ai"
                else "Напишите исправленный запрос новым сообщением."
            )
            if message_id:
                client.edit_message(chat_id, message_id, response)
            else:
                client.send_message(chat_id, response)
            client.answer_callback(callback.get("id"))
            db.commit()
            return {"ok": True, "action": action.action}
        db.commit()
        return {"ok": True, "action": action.action}
    message_data = payload.get("message") or payload.get("channel_post")
    if not message_data:
        db.commit()
        return {"ok": True, "ignored": True}
    sender = message_data.get("from", {})
    chat = message_data.get("chat", {})
    tg_id = sender.get("id") or chat.get("id")
    if not isinstance(tg_id, int):
        raise AppError("VALIDATION_ERROR", "message sender is required", 422)
    user = UserService(db).get_or_create(
        tg_id,
        telegram_username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
    )
    chat_id = chat.get("id", tg_id)
    command = (message_data.get("text") or "").split(maxsplit=1)[0].lower()
    command = command.split("@", 1)[0]
    if command == "/connect" and chat.get("type") == "private":
        TelegramClient().send_message(chat_id, "Подключить почту или календарь:", email_connect_menu(db, user))
        db.commit()
        return {"ok": True, "command": "connect"}
    if command == "/connect_mailru" and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=1)
        if len(parts) != 2:
            TelegramClient().send_message(chat_id, "Укажите адрес: /connect_mailru <email>")
            db.commit()
            return {"ok": True, "command": "connect_mailru"}
        try:
            service = OAuthService(db)
            state = service.create_state(user, "mailru", parts[1].strip())
            url = service.authorize_url("mailru", state.state_token)
            TelegramClient().send_message(
                chat_id,
                "Откройте ссылку для подключения Mail.ru:",
                {"inline_keyboard": [[{"text": "Подключить", "url": url}]]},
            )
        except AppError as exc:
            TelegramClient().send_message(chat_id, f"Не удалось начать подключение: {exc.message}")
        db.commit()
        return {"ok": True, "command": "connect_mailru"}
    if command == "/task" and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            TelegramClient().send_message(chat_id, "Укажите название задачи: /task <название>")
        else:
            text, markup = new_task_draft(db, user, parts[1])
            TelegramClient().send_message(chat_id, text, markup)
        db.commit()
        return {"ok": True, "command": "task"}
    if command == "/task_due" and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=3)
        if len(parts) != 4:
            TelegramClient().send_message(chat_id, "Формат: /task_due <token> ГГГГ-ММ-ДД ЧЧ:ММ")
        else:
            text, markup = set_task_due(db, user, parts[1], f"{parts[2]} {parts[3]}")
            TelegramClient().send_message(chat_id, text, markup)
        db.commit()
        return {"ok": True, "command": "task_due"}
    if command == "/project" and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            TelegramClient().send_message(chat_id, "Укажите название проекта: /project <название>")
        else:
            name = parts[1].strip()
            if len(name) > 255:
                raise AppError("VALIDATION_ERROR", "Название проекта слишком длинное", 422)
            create = UIActionService(db).create(user, "project_create_confirm", {"name": name})
            cancel = UIActionService(db).create(user, "cancel_ai", {})
            TelegramClient().send_message(chat_id, f"Создать проект «{name}»?", {
                "inline_keyboard": [[
                    {"text": "Создать", "callback_data": create},
                    {"text": "Отмена", "callback_data": cancel},
                ]],
            })
        db.commit()
        return {"ok": True, "command": "project"}
    if command in {"/task_edit", "/project_rename"} and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=2)
        if len(parts) != 3 or not parts[2].strip():
            TelegramClient().send_message(chat_id, f"Формат: {command} <id> <новое название>")
            db.commit()
            return {"ok": True, "command": command.removeprefix("/")}
        try:
            target_model = Task if command == "/task_edit" else Project
            object_id = resolve_target_id(db, user, parts[1], target_model)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("VALIDATION_ERROR", "Invalid object ID", 422) from exc
        title = parts[2].strip()
        if command == "/task_edit":
            if len(title) > 500:
                raise AppError("VALIDATION_ERROR", "Название задачи слишком длинное", 422)
            task, undo = TaskService(db).patch(user, object_id, {"title": title})
            text, markup = outcome_message(db, user, {
                "state": "executed", "object_id": str(task.id), "undo": undo,
            })
        else:
            if len(title) > 255:
                raise AppError("VALIDATION_ERROR", "Название проекта слишком длинное", 422)
            project = ProjectService(db).patch(user, object_id, {"name": title})
            text, markup = f"Проект переименован: {project.name}", None
        TelegramClient().send_message(chat_id, text, markup)
        db.commit()
        return {"ok": True, "command": command.removeprefix("/")}
    if command == "/reply" and chat.get("type") == "private":
        args = (message_data.get("text") or "").split(maxsplit=2)
        if len(args) < 3:
            raise AppError("VALIDATION_ERROR", "Use /reply <message-id> <text>", 422)
        try:
            target = resolve_target_id(db, user, args[1], Message)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("VALIDATION_ERROR", "Invalid message ID", 422) from exc
        token = EmailService(db).draft_for_message(user, target, args[2])
        text, markup = outcome_message(
            db,
            user,
            {
                "state": "email_draft",
                "token": token,
                "message_id": str(target),
                "draft_text": args[2],
            },
        )
        TelegramClient().send_message(chat_id, text, markup)
        db.commit()
        return {"ok": True, "draft": True}
    if command == "/clarify" and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=2)
        if len(parts) != 3:
            TelegramClient().send_message(chat_id, "Напишите: /clarify <message-id> <уточнение>")
            db.commit()
            return {"ok": True, "clarification": False}
        try:
            original_id = resolve_target_id(db, user, parts[1], Message)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("VALIDATION_ERROR", "Invalid message ID", 422) from exc
        original = db.get(Message, original_id)
        if not original or original.user_id != user.id:
            raise AppError("MESSAGE_NOT_FOUND", "Message not found", 404)
        private_source = db.scalar(select(Source).where(
            Source.user_id == user.id, Source.type == "telegram_chat",
            Source.external_source_id == str(chat_id),
        ))
        if not private_source:
            private_source = SourceService(db).create(user, {
                "type": "telegram_chat", "name": "Telegram",
                "external_source_id": str(chat_id),
            })
        refined = Message(
            user_id=user.id, source_id=private_source.id,
            external_message_id=str(message_data.get("message_id")),
            message_type="text", received_at=utcnow(), raw_payload=payload,
            text=(f"Исходное сообщение (ID {original.id}):\n"
                  f"{original.subject or ''}\n{original.text or ''}\n"
                  f"Уточнение пользователя: {parts[2]}"),
            processing_status="queued",
        )
        db.add(refined)
        db.flush()
        job = AIProcessingJob(user_id=user.id, message_id=refined.id, job_type="interpret", status="queued")
        sent = TelegramClient().send_message(chat_id, "🤔 Уточняю запрос...")
        if sent:
            job.status_message_id = sent.get("message_id")
        db.add(job)
        db.commit()
        from app.workers.tasks import process_message

        try:
            process_message.delay(str(job.id))
        except Exception:
            pass
        return {"ok": True, "clarification": True, "job_id": str(job.id)}
    if command == "/timezone" and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=1)
        if len(parts) != 2:
            response = "Укажите часовой пояс: /timezone Europe/Moscow"
        else:
            try:
                ZoneInfo(parts[1].strip())
            except ZoneInfoNotFoundError:
                response = "Неизвестный часовой пояс. Например: /timezone Europe/Moscow"
            else:
                user.timezone = parts[1].strip()
                response = f"Часовой пояс установлен: {user.timezone}"
        TelegramClient().send_message(chat_id, response)
        db.commit()
        return {"ok": True, "command": "timezone"}
    if command in {"/briefing_time", "/stats_time"} and chat.get("type") == "private":
        parts = (message_data.get("text") or "").split(maxsplit=1)
        if len(parts) != 2:
            response = f"Укажите время: {command} 08:00"
        else:
            try:
                scheduled = time.fromisoformat(parts[1].strip())
            except ValueError:
                response = "Неизвестное время. Используйте формат ЧЧ:ММ."
            else:
                settings = db.get(UserSettings, user.id)
                field = "morning_briefing_time" if command == "/briefing_time" else "evening_stats_time"
                setattr(settings, field, scheduled)
                response = f"Время обновлено: {scheduled:%H:%M} ({user.timezone})."
        TelegramClient().send_message(chat_id, response)
        db.commit()
        return {"ok": True, "command": command.removeprefix("/")}
    if command == "/connect" and chat.get("type") in {"group", "supergroup"}:
        member = TelegramClient().get_chat_member(chat_id, tg_id)
        if not member or member.get("status") not in {"creator", "administrator"}:
            TelegramClient().send_message(chat_id, "Подключить группу может только администратор.")
            db.commit()
            return {"ok": True, "connected": False}
        existing = db.scalar(
            select(Source).where(
                Source.user_id == user.id,
                Source.type == "telegram_group",
                Source.external_source_id == str(chat_id),
            )
        )
        if existing and existing.status == "active":
            TelegramClient().send_message(chat_id, "Группа уже подключена.")
            db.commit()
            return {"ok": True, "connected": True, "source_id": str(existing.id)}
        source = SourceService(db).create(
            user,
            {
                "type": "telegram_group",
                "name": chat.get("title") or "Telegram group",
                "external_source_id": str(chat_id),
            },
        )
        TelegramClient().send_message(
            chat_id, "Группа подключена. Новые сообщения будут анализироваться."
        )
        return {"ok": True, "connected": True, "source_id": str(source.id)}
    if command.startswith("/") and chat.get("type") != "private":
        db.commit()
        return {"ok": True, "ignored": True, "reason": "private_command"}
    if command == "/api_token":
        if chat.get("type") != "private":
            return {"ok": True, "ignored": True}
        TelegramClient().send_message(
            chat_id, f"Ваш API token (24 часа):\n{issue_user_token(tg_id)}"
        )
        db.commit()
        return {"ok": True, "command": "api_token"}
    if command in {"/start", "/menu"}:
        action_service = UIActionService(db)
        tokens = {
            section: action_service.create(
                user, "navigate", {"section": section}, ttl_seconds=86400
            )
            for section in (
                "today",
                "tasks",
                "projects",
                "inbox",
                "sources",
                "schedule",
                "settings",
            )
        }
        if command == "/start":
            text = (
                "👋 Привет! Я TaskMate AI — ваш умный помощник по управлению задачами и календарем.\n\n"
                "💡 Как со мной работать:\n"
                "• Подключите источники через /sources, из которых я буду распознавать ваши задачи и письма из email, "
                "или просто пишите и надиктовывайте голосом задачи: «Купить билеты в пятницу», «Встреча с командой в 12:00».\n"
                "• Чтобы задачи не скапливались только во Входящих — привяжите источники к проектам в /projects, "
                "и поручения будут автоматически распределяться по нужным проектам.\n"
                "• Планируйте встречи и добавляйте события в календарь: /schedule.\n"
                "• Получайте утренние сводки дня (/today) и напоминания в точное время согласно вашему часовому поясу, "
                "настроить который можно в /settings.\n\n"
                "🎯 С чего начать:\n"
                "1. Зайдите в /settings ⚙️ и выберите ваш город/часовой пояс (по умолчанию установлен UTC).\n"
                "2. Подключите email или Telegram-группу через /sources либо просто отправьте мне вашу первую задачу текстом или голосом.\n\n"
                "Выберите нужный раздел для старта:"
            )
        else:
            text = "TaskMate AI готов. Выберите раздел:"
        TelegramClient().send_message(
            chat_id, text, main_menu(tokens)
        )
        db.commit()
        return {"ok": True, "command": command.removeprefix("/")}
    if command in {
        "/today",
        "/schedule",
        "/stats",
        "/analyze",
        "/settings",
        "/tasks",
        "/projects",
        "/inbox",
        "/sources",
    }:
        sec = command.removeprefix("/")
        markup = None
        if command == "/analyze":
            analysis = queue_source_analysis(db, user)
            response = (
                "Пока нет подключённых источников для анализа."
                if analysis["status"] == "no_sources"
                else f"Анализ запущен для сообщений: {analysis['queued']}"
            )
            if analysis.get("external_sync") == "queued":
                response += "\nСинхронизация почты запущена в фоне."
            elif analysis.get("external_sync") == "broker_unavailable":
                response += "\nОчередь синхронизации почты недоступна."
        elif sec in {"tasks", "projects", "inbox", "sources", "settings"}:
            response, markup = section_view(db, user, sec)
        else:
            response = section_message(db, user, sec)
        TelegramClient().send_message(chat_id, response, markup)
        db.commit()
        return {"ok": True, "command": sec}
    chat_type = chat.get("type", "private")
    source_type = {
        "private": "telegram_chat",
        "group": "telegram_group",
        "supergroup": "telegram_group",
        "channel": "telegram_channel",
    }.get(chat_type, "telegram_chat")
    external_id = str(chat.get("id", tg_id))
    source_query = select(Source).where(
        Source.type == source_type, Source.external_source_id == external_id
    )
    if chat_type == "private":
        source_query = source_query.where(Source.user_id == user.id)
    source = db.scalar(source_query.order_by(Source.connected_at.desc()))
    created_source = source is None
    if not source:
        if chat_type != "private":
            db.commit()
            return {"ok": True, "ignored": True, "reason": "source_not_connected"}
        source = SourceService(db).create(
            user,
            {
                "type": source_type,
                "name": chat.get("title") or "Telegram",
                "external_source_id": external_id,
            },
        )
    if source.status != "active":
        db.commit()
        return {"ok": True, "ignored": True, "reason": "source_inactive"}
    if chat_type != "private":
        user = db.get(User, source.user_id)
    message_date = message_data.get("date")
    received_at = (
        datetime.fromtimestamp(message_date, UTC)
        if isinstance(message_date, int)
        else utcnow()
    )
    connected_at = source.connected_at
    if connected_at and not created_source:
        connected_at = connected_at.replace(tzinfo=connected_at.tzinfo or UTC)
        if received_at <= connected_at:
            db.commit()
            return {"ok": True, "ignored": True, "reason": "before_source_connected"}
    is_voice = "voice" in message_data
    has_attachment = any(key in message_data for key in ("voice", "photo", "document"))
    external_message_id = str(message_data.get("message_id"))
    if db.scalar(
        select(Message).where(
            Message.source_id == source.id, Message.external_message_id == external_message_id
        )
    ):
        db.commit()
        return {"ok": True, "duplicate": True}
    text = message_data.get("text") or message_data.get("caption")
    message_type = next(
        (kind for kind in ("voice", "document", "photo") if kind in message_data), "text"
    )
    needs_ai = (is_voice and source.analysis_voice) or (
        not is_voice and bool(text) and source.analysis_text
    )
    needs_job = bool(needs_ai or (has_attachment and source.save_attachments))
    message = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id=external_message_id,
        sender_external_id=str(sender.get("id", "")),
        sender_name=sender.get("first_name"),
        text=text,
        message_type=message_type,
        received_at=received_at,
        raw_payload=payload,
        processing_status="queued" if needs_job else "ignored",
    )
    db.add(message)
    db.flush()
    if not needs_job:
        db.commit()
        return {
            "ok": True,
            "ignored": True,
            "reason": "source_analysis_disabled"
            if not source.analysis_text and not source.analysis_voice and not source.save_attachments
            else "no_analyzable_content",
        }
    job = AIProcessingJob(
        user_id=user.id,
        message_id=message.id,
        job_type="stt"
        if is_voice and needs_ai
        else ("interpret" if needs_ai else "attachment_only"),
        status="queued",
    )
    status_message = TelegramClient().send_message(
        chat_id,
        "🎙 Обрабатываю голосовое..."
        if is_voice
        else (
            "📎 Сохраняю вложение..."
            if has_attachment and not text
            else "🤔 Анализирую сообщение..."
        ),
    )
    if status_message:
        job.status_message_id = status_message.get("message_id")
    db.add(job)
    db.commit()
    try:
        from app.workers.tasks import process_message

        process_message.delay(str(job.id))
    except Exception:
        # The durable queued job remains available for a worker after Redis recovery.
        pass
    return {"ok": True, "job_id": str(job.id)}
