import uuid

from fastapi import APIRouter, Header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai.service import AIActionService
from app.api.deps import DB
from app.bot.presentation import outcome_message, section_message
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import issue_user_token
from app.integrations.telegram.client import TelegramClient, main_menu
from app.models.entities import AIProcessingJob, Message, Source, TelegramUpdate, User
from app.schemas.domain import AIResult
from app.services.analysis import queue_source_analysis
from app.services.domain import SourceService, TaskService, UIActionService, UserService, utcnow
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
                for provider, label in labels[2:]
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


@router.post("/webhooks/telegram")
def telegram_webhook(
    payload: dict,
    db: DB,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    if secret != get_settings().telegram_webhook_secret:
        raise AppError("AUTH_ERROR", "Invalid Telegram webhook secret", 401)
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
        if action.action == "confirm_ai":
            message = (
                db.get(Message, uuid.UUID(action.payload["message_id"]))
                if action.payload.get("message_id")
                else None
            )
            outcome = AIActionService(db).execute(
                user, AIResult.model_validate(action.payload["result"]), message
            )
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
            client.send_message(
                chat_id,
                section_message(db, user, section),
                email_connect_menu(db, user)
                if section == "sources"
                and callback_message.get("chat", {}).get("type") == "private"
                else None,
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
            if provider in {"gmail", "yandex"}:
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
                client.send_message(
                    chat_id,
                    (
                        "Не удалось подтвердить отправку. Проверьте исходящие письма перед повтором. "
                        f"Черновик: {action.payload['draft_text'][:2000]}\n"
                        f"Новый ответ: /reply {action.payload['message_id']} <текст>. "
                        f"Причина: {exc.message}"
                    ),
                )
                client.answer_callback(callback.get("id"))
                return {"ok": True, "sent": False, "error": exc.code}
            client.send_message(chat_id, "Ответ отправлен.")
            client.answer_callback(callback.get("id"))
            return {"ok": True, "sent": sent}
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
        TelegramClient().send_message(chat_id, "Подключить почту:", email_connect_menu(db, user))
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
    if command == "/reply" and chat.get("type") == "private":
        args = (message_data.get("text") or "").split(maxsplit=2)
        if len(args) < 3:
            raise AppError("VALIDATION_ERROR", "Use /reply <message-id> <text>", 422)
        try:
            target = uuid.UUID(args[1])
        except ValueError as exc:
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
        TelegramClient().send_message(
            chat_id, "TaskMate AI готов. Выберите раздел:", main_menu(tokens)
        )
        db.commit()
        return {"ok": True, "command": command.removeprefix("/")}
    if command in {"/today", "/schedule", "/stats", "/analyze", "/settings"}:
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
        else:
            response = section_message(db, user, command.removeprefix("/"))
        TelegramClient().send_message(chat_id, response)
        db.commit()
        return {"ok": True, "command": command.removeprefix("/")}
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
    is_voice = "voice" in message_data
    has_attachment = any(key in message_data for key in ("voice", "photo", "document"))
    if (is_voice and not source.analysis_voice and not source.save_attachments) or (
        not is_voice
        and not source.analysis_text
        and not (has_attachment and source.save_attachments)
    ):
        db.commit()
        return {"ok": True, "ignored": True, "reason": "source_analysis_disabled"}
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
        received_at=utcnow(),
        raw_payload=payload,
        processing_status="queued" if needs_job else "ignored",
    )
    db.add(message)
    db.flush()
    if not needs_job:
        db.commit()
        return {"ok": True, "ignored": True, "reason": "no_analyzable_content"}
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
