from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.bot.actions import handle_action
from app.bot.manual import edit_task_draft, handle_task_draft_action, new_task_draft, set_task_due
from app.bot.views import object_view, section_view
from app.core.errors import AppError
from app.integrations.telegram.client import TelegramClient
from app.models.entities import Project, Task
from app.services.domain import ProjectService, TaskService, UIActionService, UserService


def test_manual_task_draft_fields_and_creation(client, db, monkeypatch):
    user = UserService(db).get_or_create(7331)
    user.timezone = "Europe/Moscow"
    db.commit()
    send = Mock(return_value={"message_id": 10})
    edit = Mock()
    monkeypatch.setattr(TelegramClient, "send_message", send)
    monkeypatch.setattr(TelegramClient, "edit_message", edit)
    monkeypatch.setattr(TelegramClient, "answer_callback", Mock())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    def command(update_id, text):
        return client.post("/webhooks/telegram", headers=headers, json={
            "update_id": update_id, "message": {
                "message_id": update_id, "from": {"id": 7331},
                "chat": {"id": 7331, "type": "private"}, "text": text,
            },
        })

    def callback(update_id, token):
        return client.post("/webhooks/telegram", headers=headers, json={
            "update_id": update_id, "callback_query": {
                "id": str(update_id), "from": {"id": 7331}, "data": token,
                "message": {"message_id": 10, "chat": {"id": 7331, "type": "private"}},
            },
        })

    text, markup = section_view(db, user, "tasks")
    assert markup["inline_keyboard"][0][0]["text"] == "Создать задачу"
    assert command(1, "/task Подготовить презентацию").status_code == 200
    draft_markup = send.call_args.args[2]
    assert send.call_args.args[1].startswith("Новая задача")
    priority_token = draft_markup["inline_keyboard"][2][0]["callback_data"]
    assert callback(2, priority_token).json()["action"] == "task_draft_priority"
    high_token = edit.call_args.args[3]["inline_keyboard"][2][0]["callback_data"]
    assert callback(3, high_token).json()["action"] == "task_draft_set_priority"
    draft_markup = edit.call_args.args[3]
    due_token = draft_markup["inline_keyboard"][1][0]["callback_data"]
    assert callback(4, due_token).json()["action"] == "task_draft_due"
    input_token = edit.call_args.args[2].split()[3]
    assert command(5, f"/task_due {input_token} 2026-10-15 14:00").status_code == 200
    draft_markup = send.call_args.args[2]
    create_token = draft_markup["inline_keyboard"][0][0]["callback_data"]
    assert callback(6, create_token).json()["action"] == "task_draft_create"
    task = db.scalar(select(Task).where(Task.user_id == user.id))
    assert task.title == "Подготовить презентацию"
    assert task.priority == "high"
    assert task.due_at.replace(tzinfo=UTC) == datetime(2026, 10, 15, 11, 0, tzinfo=UTC)
    assert task.project_id is None


def test_manual_project_and_task_card_actions(db):
    user = UserService(db).get_or_create(7332)
    text, markup = section_view(db, user, "projects")
    assert markup["inline_keyboard"][0][0]["text"] == "Создать проект"
    create = UIActionService(db).create(user, "project_create_confirm", {"name": "Marketing"})
    text, _ = handle_action(db, user, UIActionService(db).consume(user, create))
    assert "создан" in text
    project = db.scalar(select(Project).where(Project.user_id == user.id))
    task, _ = TaskService(db).create(user, {"title": "Draft", "project_id": project.id})
    _, markup = object_view(db, user, "tasks", task.id)
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        "Выполнить", "Изменить", "Отложить на 1 день", "Удалить…",
    ]
    prompt = UIActionService(db).consume(user, markup["inline_keyboard"][0][0]["callback_data"])
    text, confirm = handle_action(db, user, prompt)
    assert "выполненной" in text
    assert task.status != "completed"
    action = UIActionService(db).consume(user, confirm["inline_keyboard"][0][0]["callback_data"])
    handle_action(db, user, action)
    assert task.status == "completed"
    _, markup = object_view(db, user, "projects", project.id)
    delete_token = markup["inline_keyboard"][2][0]["callback_data"]
    text, confirm = handle_action(db, user, UIActionService(db).consume(user, delete_token))
    assert "нельзя отменить" in text
    assert db.get(Project, project.id)
    confirm_token = confirm["inline_keyboard"][0][0]["callback_data"]
    handle_action(db, user, UIActionService(db).consume(user, confirm_token))
    assert db.get(Project, project.id) is None
    db.refresh(task)
    assert task.project_id is None


def test_manual_draft_project_priority_and_invalid_input(db):
    user = UserService(db).get_or_create(7333)
    project = ProjectService(db).create(user, {"name": "Alpha"})
    with pytest.raises(AppError):
        new_task_draft(db, user, "  ")
    _, markup = new_task_draft(db, user, "New task")
    draft = UIActionService(db).consume(
        user, markup["inline_keyboard"][0][0]["callback_data"],
    ).payload["draft"]
    _, project_markup = handle_task_draft_action(db, user, "task_draft_project", draft, {})
    assert [row[0]["text"] for row in project_markup["inline_keyboard"]] == ["Inbox", "Alpha"]
    text, _ = handle_task_draft_action(db, user, "task_draft_set_project", draft, {
        "project_id": str(project.id),
    })
    assert "Проект: Alpha" in text
    _, priority_markup = handle_task_draft_action(db, user, "task_draft_priority", draft, {})
    assert len(priority_markup["inline_keyboard"]) == 3
    text, _ = handle_task_draft_action(db, user, "task_draft_set_priority", draft, {
        "priority": "low",
    })
    assert "Приоритет: low" in text
    with pytest.raises(AppError):
        handle_task_draft_action(db, user, "task_draft_set_priority", draft, {
            "priority": "urgent",
        })
    with pytest.raises(AppError):
        set_task_due(db, user, "invalid", "not a date")


def test_manual_telegram_project_and_edit_commands(client, db, monkeypatch):
    user = UserService(db).get_or_create(7334)
    task, _ = TaskService(db).create(user, {"title": "Old task"})
    send = Mock(return_value={"message_id": 10})
    monkeypatch.setattr(TelegramClient, "send_message", send)
    monkeypatch.setattr(TelegramClient, "edit_message", Mock())
    monkeypatch.setattr(TelegramClient, "answer_callback", Mock())
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}

    def command(update_id, text):
        return client.post("/webhooks/telegram", headers=headers, json={
            "update_id": update_id, "message": {
                "message_id": update_id, "from": {"id": 7334},
                "chat": {"id": 7334, "type": "private"}, "text": text,
            },
        })

    assert command(81, "/project Marketing").status_code == 200
    create = send.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
    response = client.post("/webhooks/telegram", headers=headers, json={
        "update_id": 82, "callback_query": {
            "id": "82", "from": {"id": 7334}, "data": create,
            "message": {"message_id": 10, "chat": {"id": 7334, "type": "private"}},
        },
    })
    assert response.status_code == 200
    project = db.scalar(select(Project).where(Project.user_id == user.id))
    assert command(83, f"/project_rename {project.id} Sales").status_code == 200
    assert project.name == "Sales"
    assert command(84, f"/task_edit {task.id} New task").status_code == 200
    assert task.title == "New task"


def test_existing_task_can_edit_priority_project_and_due_date(db):
    user = UserService(db).get_or_create(7335)
    project = ProjectService(db).create(user, {"name": "Work"})
    task, _ = TaskService(db).create(user, {"title": "Existing"})
    text, markup = edit_task_draft(db, user, task.id)
    assert text.startswith("Изменение задачи")
    token = markup["inline_keyboard"][0][0]["callback_data"]
    draft = UIActionService(db).consume(user, token).payload["draft"]
    assert draft["task_id"] == str(task.id)
    draft.update({
        "priority": "high", "project_id": str(project.id),
        "due_at": "2026-10-15T11:00:00+00:00",
    })
    handle_task_draft_action(db, user, "task_draft_create", draft, {})
    assert task.priority == "high" and task.project_id == project.id
    assert task.due_at.replace(tzinfo=UTC) == datetime(2026, 10, 15, 11, tzinfo=UTC)
