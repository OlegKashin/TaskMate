import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, not_found
from app.models.entities import (
    AIProcessingJob,
    CalendarConnection,
    InboxItem,
    Message,
    Notification,
    Source,
    Task,
    User,
)
from app.schemas.domain import AIAction, AIResult
from app.services.domain import (
    CalendarService,
    ProjectService,
    ReminderService,
    SourceService,
    TaskService,
    WaitingService,
)

MUTATING_INTENTS = {
    "create_task", "edit_task", "delete_task", "change_task_status", "create_event",
    "project_action", "source_action", "reminder", "reply_email", "create_waiting_for",
}


class LLMProvider(Protocol):
    async def interpret(self, text: str) -> AIResult: ...


class RuleBasedProvider:
    """Offline-safe provider used for development and deterministic tests."""

    async def interpret(self, text: str) -> AIResult:
        raw, lowered = text.strip(), text.lower()
        if any(word in lowered for word in ("напомни", "remind")):
            return AIResult(
                intent="reminder",
                confidence=0.9,
                entities={
                    "title": raw,
                    "due_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
                action=AIAction(type="reminder"),
                reason="Detected reminder command",
            )
        if any(word in lowered for word in ("жду", "ожидаю", "waiting")):
            return AIResult(
                intent="create_waiting_for",
                confidence=0.9,
                entities={"title": raw},
                action=AIAction(type="create_waiting_for"),
                reason="Detected waiting-for command",
            )
        if any(
            word in lowered for word in ("задач", "сделать", "todo", "отправить", "подготовить")
        ):
            title = re.sub(r"^(создай\s+)?задач[ауи]?\s*", "", raw, flags=re.I) or raw
            return AIResult(
                intent="create_task",
                confidence=0.9,
                entities={"title": title, "priority": "normal"},
                action=AIAction(type="create_task"),
                reason="Detected task command",
            )
        return AIResult(
            intent="general_query",
            confidence=0.7,
            entities={},
            action=AIAction(type="none", requires_confirmation=False),
            reason="No mutating intent detected",
        )


class AIActionService:
    def __init__(self, db: Session):
        self.db = db

    def apply(self, user: User, result: AIResult, message: Message | None = None):
        if result.confidence < 0.60:
            return {"state": "clarification", "reason": result.reason}
        if result.intent == "create_event" and not result.entities.get("connection_id"):
            active_calendar = self.db.scalar(select(CalendarConnection.id).where(
                CalendarConnection.user_id == user.id,
                CalendarConnection.status == "active",
            ))
            if not active_calendar:
                return {"state": "calendar_not_connected"}
        if result.intent == "reply_email":
            from app.services.domain import UIActionService

            token = UIActionService(self.db).create(user, "confirm_ai", {
                "result": result.model_dump(mode="json"),
                "message_id": str(message.id) if message else None,
            })
            return {"state": "proposal", "confidence": "high" if result.confidence >= 0.85 else "medium", "token": token,
                    "intent": result.intent, "entities": result.entities, "reason": result.reason}
        if result.intent in MUTATING_INTENTS:
            from app.services.domain import UIActionService

            token = UIActionService(self.db).create(
                user,
                "confirm_ai",
                {
                    "result": result.model_dump(mode="json"),
                    "message_id": str(message.id) if message else None,
                },
            )
            return {
                "state": "proposal",
                "confidence": "high" if result.confidence >= 0.85 else "medium",
                "token": token,
                "intent": result.intent,
                "entities": result.entities,
                "reason": result.reason,
            }
        return self.execute(user, result, message)

    def _target_task(self, user: User, target: dict) -> Task | list[Task]:
        if target.get("task_id"):
            return TaskService(self.db).get(user, uuid.UUID(target["task_id"]))
        hint = target.get("search_hint", {}).get("title_contains", "").strip()
        if not hint:
            raise AppError("VALIDATION_ERROR", "Specify a task to change", 422)
        candidates = self.db.scalars(
            select(Task).where(Task.user_id == user.id, Task.deleted_at.is_(None),
                               Task.title.ilike(f"%{hint}%")).limit(6)
        ).all()
        if not candidates:
            raise not_found("task")
        return candidates[0] if len(candidates) == 1 else candidates

    def execute(self, user: User, result: AIResult, message: Message | None = None):
        entities = dict(result.entities)
        if result.intent == "create_task":
            values = {
                key: entities.get(key)
                for key in ("title", "description", "due_at", "priority")
                if entities.get(key) is not None
            }
            values.setdefault("priority", "normal")
            if isinstance(values.get("due_at"), str):
                values["due_at"] = datetime.fromisoformat(values["due_at"])
            hint = entities.get("project_hint")
            if hint:
                from app.models.entities import Project

                matches = self.db.scalars(
                    select(Project).where(Project.user_id == user.id,
                                          Project.is_archived.is_(False),
                                          Project.name.ilike(f"%{hint}%")).limit(2)
                ).all()
                if len(matches) > 1:
                    return {"state": "clarification", "reason": "Выберите проект"}
                if matches:
                    values["project_id"] = matches[0].id
            if message:
                values["source_message_id"] = message.id
            task, undo = TaskService(self.db).create(user, values, "ai")
            return {"state": "executed", "object_id": str(task.id), "undo": undo}
        if result.intent in {"edit_task", "delete_task", "change_task_status"}:
            target = self._target_task(user, entities["target"])
            if isinstance(target, list):
                return {"state": "clarification", "reason": "Найдено несколько задач",
                        "candidates": [{"id": str(item.id), "title": item.title} for item in target]}
            service = TaskService(self.db)
            if result.intent == "delete_task":
                task, undo = service.delete(user, target.id)
            elif result.intent == "change_task_status":
                task, undo = service.change_status(user, target.id, entities["new_status"])
            else:
                changes = dict(entities["changed_fields"])
                if "project_hint" in changes:
                    hint = changes.pop("project_hint")
                    if hint is None:
                        changes["project_id"] = None
                    else:
                        from app.models.entities import Project

                        matches = self.db.scalars(
                            select(Project).where(Project.user_id == user.id,
                                                  Project.is_archived.is_(False),
                                                  Project.name.ilike(f"%{hint}%")).limit(2)
                        ).all()
                        if len(matches) != 1:
                            return {"state": "clarification", "reason": "Выберите проект"}
                        changes["project_id"] = matches[0].id
                if isinstance(changes.get("due_at"), str):
                    changes["due_at"] = datetime.fromisoformat(changes["due_at"])
                task, undo = service.patch(user, target.id, changes, "ai")
            return {"state": "executed", "object_id": str(task.id), "undo": undo}
        if result.intent == "create_waiting_for":
            waiting_due_at = entities.get("due_at")
            if isinstance(waiting_due_at, str):
                waiting_due_at = datetime.fromisoformat(waiting_due_at)
            values = {
                "title": entities["title"],
                "expected_from": entities.get("expected_from"),
                "due_at": waiting_due_at,
                "message_id": message.id if message else None,
            }
            obj = WaitingService(self.db).create(user, values)
            return {"state": "executed", "object_id": str(obj.id)}
        if result.intent == "reminder":
            due_at = entities["due_at"]
            if isinstance(due_at, str):
                due_at = datetime.fromisoformat(due_at)
            obj = ReminderService(self.db).create(
                user,
                {
                    "title": entities["title"],
                    "due_at": due_at,
                    "task_id": entities.get("related_task_id"),
                },
            )
            return {"state": "executed", "object_id": str(obj.id)}
        if result.intent == "project_action":
            action = entities["action"]
            service = ProjectService(self.db)
            if action == "create":
                obj = service.create(user, {"name": entities["project_name"]})
            else:
                project_id = uuid.UUID(entities["project_id"])
                if action == "delete":
                    service.delete(user, project_id)
                    return {"state": "executed", "object_id": str(project_id)}
                values = {"name": entities["new_name"]} if action == "rename" else {"is_archived": True}
                obj = service.patch(user, project_id, values)
            return {"state": "executed", "object_id": str(obj.id)}
        if result.intent == "source_action":
            source_id = uuid.UUID(entities["source_id"])
            service = SourceService(self.db)
            action = entities["action"]
            if action == "disconnect":
                obj = service.disconnect(user, source_id)
            else:
                values = {"status": "paused" if action == "pause" else "active"} if action != "change_settings" else entities.get("settings", {})
                obj = service.patch(user, source_id, values)
            return {"state": "executed", "object_id": str(obj.id)}
        if result.intent == "create_event":
            connection_id = entities.get("connection_id")
            if not connection_id:
                connections = self.db.scalars(select(CalendarConnection).where(
                    CalendarConnection.user_id == user.id,
                    CalendarConnection.status == "active",
                ).limit(2)).all()
                if not connections:
                    return {"state": "calendar_not_connected"}
                if len(connections) != 1:
                    return {"state": "clarification", "reason": "Выберите подключённый календарь"}
                connection_id = connections[0].id
            values = {
                "connection_id": uuid.UUID(str(connection_id)),
                "title": entities["title"],
                "description": entities.get("description"),
                "start_at": datetime.fromisoformat(entities["start_at"]),
                "end_at": datetime.fromisoformat(entities["end_at"]),
            }
            if entities.get("task_id"):
                values["task_id"] = uuid.UUID(entities["task_id"])
            event = CalendarService(self.db).create(user, values)
            return {"state": "executed", "object_id": str(event.id)}
        if result.intent == "reply_email":
            from app.services.email import EmailService

            try:
                target = uuid.UUID(str(entities["message_id"]))
            except (ValueError, KeyError) as exc:
                raise AppError("VALIDATION_ERROR", "Valid email message ID required", 422) from exc
            token = EmailService(self.db).draft_for_message(user, target, entities["draft_text"])
            return {"state": "email_draft", "token": token,
                    "message_id": str(target), "draft_text": entities["draft_text"]}
        if result.intent in {"search_task", "show_today", "show_schedule", "show_stats", "search_event", "analyze"}:
            return {"state": "informational", "intent": result.intent, "entities": entities}
        return {"state": "informational", "reason": result.reason}


class AIService:
    def __init__(self, db: Session, provider: LLMProvider | None = None):
        self.db = db
        if provider is not None:
            self.provider = provider
        elif get_settings().llm_provider == "openai":
            from app.ai.openai_provider import OpenAIProvider

            self.provider = OpenAIProvider()
        elif get_settings().llm_provider == "local":
            self.provider = RuleBasedProvider()
        else:
            raise AppError("LLM_NOT_CONFIGURED", "Unsupported LLM_PROVIDER", 503)

    async def process(self, job: AIProcessingJob, message: Message, user: User):
        job.status, job.attempts = "processing", job.attempts + 1
        message.processing_status = "processing"
        self.db.commit()
        try:
            if message.message_type == "voice" and not message.text:
                from app.ai.transcription import VoiceTranscriber

                message.text = await VoiceTranscriber().transcribe(message)
            input_text = message.text or message.subject or ""
            if message.message_type == "email":
                input_text = (
                    f"Email message_id: {message.id}\n"
                    f"From: {message.sender_email or ''}\n"
                    f"Subject: {message.subject or ''}\n"
                    f"Body:\n{input_text}"
                )
            result = await self.provider.interpret(input_text)
            outcome = AIActionService(self.db).apply(user, result, message)
            job.result, job.status, message.processing_status = (
                {"ai": result.model_dump(mode="json"), "outcome": outcome},
                "completed",
                "processed",
            )
            inferred_types = {
                "create_task": "task_candidate",
                "reply_email": "reply_required",
                "create_waiting_for": "waiting_for",
                "create_event": "meeting",
            }
            inferred_type = inferred_types.get(result.intent) if outcome.get("state") in {
                "proposal", "calendar_not_connected",
            } else None
            if inferred_type:
                self.db.add(
                    InboxItem(
                        user_id=user.id,
                        message_id=message.id,
                        item_type=inferred_type,
                        status="proposed",
                        title=message.subject or result.entities.get("title"),
                        summary=message.text,
                    )
                )
            if outcome.get("state") == "clarification" and not result.inbox_classification:
                source = self.db.get(Source, message.source_id)
                if source and source.type in {"gmail", "yandex", "mailru", "imap"}:
                    self.db.add(InboxItem(
                        user_id=user.id, message_id=message.id, item_type="question",
                        title=message.subject or "Нужно уточнение", summary=result.reason,
                    ))
                    self.db.add(Notification(
                        user_id=user.id, type="clarification_needed",
                        payload={"title": f"Нужно уточнить письмо «{message.subject or 'без темы'}» в Inbox."},
                        dedupe_key=f"clarify:{message.id}",
                    ))
            if result.inbox_classification:
                classification = result.inbox_classification
                self.db.add(
                    InboxItem(
                        user_id=user.id,
                        message_id=message.id,
                        item_type=classification.item_type,
                        title=message.subject,
                        summary=message.text,
                        priority=classification.priority,
                    )
                )
                if classification.item_type == "urgent":
                    self.db.add(
                        Notification(
                            user_id=user.id,
                            type="urgent_item",
                            payload={"message_id": str(message.id)},
                            dedupe_key=f"urgent:{message.id}",
                        )
                    )
            self.db.commit()
            return outcome
        except Exception as exc:
            job.status = "failed"
            job.error_code = exc.code if isinstance(exc, AppError) else "AI_ERROR"
            message.processing_status = "failed"
            self.db.commit()
            raise
