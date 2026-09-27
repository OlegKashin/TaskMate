import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserPatch(BaseModel):
    timezone: str | None = None
    locale: str | None = None
    telegram_username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_archived: bool | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    project_id: uuid.UUID | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    due_at: datetime | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    project_id: uuid.UUID | None = None
    priority: Literal["low", "normal", "high"] | None = None
    due_at: datetime | None = None


class SourceCreate(BaseModel):
    type: Literal[
        "telegram_chat", "telegram_group", "telegram_channel", "gmail", "yandex", "mailru", "imap"
    ]
    name: str = Field(min_length=1, max_length=255)
    external_source_id: str | None = None


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "paused", "error"] | None = None
    analysis_text: bool | None = None
    analysis_voice: bool | None = None
    save_attachments: bool | None = None


class SourceProjectsPatch(BaseModel):
    project_ids: list[uuid.UUID]


class FolderSelection(BaseModel):
    external_folder_id: str
    is_selected: bool


class FolderPatch(BaseModel):
    folders: list[FolderSelection]


class InboxSnooze(BaseModel):
    until: datetime


class EmailReply(BaseModel):
    draft_text: str = Field(min_length=1)


class ConfirmationToken(BaseModel):
    token: str = Field(min_length=1)


class IMAPCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1)


class WaitingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    expected_from: str | None = None
    due_at: datetime | None = None


class WaitingPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    expected_from: str | None = None
    due_at: datetime | None = None


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    due_at: datetime
    task_id: uuid.UUID | None = None


class ReminderPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None


class EventCreate(BaseModel):
    connection_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    start_at: datetime
    end_at: datetime
    task_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def valid_period(self):
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class AIAction(BaseModel):
    type: str
    requires_confirmation: bool = True


INTENTS = Literal[
    "general_query",
    "create_task",
    "edit_task",
    "delete_task",
    "search_task",
    "change_task_status",
    "create_event",
    "search_event",
    "project_action",
    "source_action",
    "analyze",
    "show_today",
    "show_schedule",
    "show_stats",
    "reminder",
    "reply_email",
    "create_waiting_for",
    "ignore",
]


class InboxClassification(BaseModel):
    item_type: Literal[
        "urgent", "opportunity", "question", "request", "approval", "promise",
        "risk", "financial", "legal", "personal",
    ]
    priority: Literal["low", "normal", "high"] = "normal"


class AIResult(BaseModel):
    intent: INTENTS
    confidence: float = Field(ge=0, le=1)
    entities: dict[str, Any] = Field(default_factory=dict)
    action: AIAction
    reason: str = ""
    inbox_classification: InboxClassification | None = None

    @model_validator(mode="after")
    def validate_intent(self):
        required = {
            "create_task": ("title",),
            "edit_task": ("target", "changed_fields"),
            "delete_task": ("target",),
            "change_task_status": ("target", "new_status"),
            "create_event": ("title", "start_at", "end_at"),
            "project_action": ("action",),
            "source_action": ("action", "source_id"),
            "reminder": ("title", "due_at"),
            "reply_email": ("message_id", "draft_text"),
            "create_waiting_for": ("title",),
        }
        missing = [key for key in required.get(self.intent, ()) if self.entities.get(key) is None]
        if missing and self.confidence >= 0.60:
            raise ValueError(f"Missing entities for {self.intent}: {', '.join(missing)}")
        if self.action.type not in {self.intent, "none"}:
            raise ValueError("action.type does not match intent")
        if self.intent == "change_task_status" and self.entities.get("new_status") is not None and self.entities["new_status"] not in {
            "new", "in_progress", "completed", "cancelled"
        }:
            raise ValueError("Invalid task status")
        if self.intent == "project_action" and self.entities.get("action") is not None and self.entities["action"] not in {
            "create", "rename", "archive", "delete"
        }:
            raise ValueError("Invalid project action")
        if self.intent == "source_action" and self.entities.get("action") is not None and self.entities["action"] not in {
            "pause", "resume", "disconnect", "change_settings"
        }:
            raise ValueError("Invalid source action")
        return self


class AnalyzeRequest(BaseModel):
    source_id: uuid.UUID | None = None


class TelegramUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    update_id: int
