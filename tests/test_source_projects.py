import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

from sqlalchemy import select

from app.ai.service import AIActionService
from app.bot.actions import handle_action
from app.bot.presentation import outcome_message
from app.bot.views import object_view
from app.integrations.telegram.client import TelegramClient
from app.models.entities import Message, Task, UIAction
from app.schemas.domain import AIAction, AIResult
from app.services.domain import ProjectService, SourceService, UIActionService, UserService


def test_source_project_api_validates_ownership_and_archive(client, db, headers):
    user = UserService(db).get_or_create(1001)
    other = UserService(db).get_or_create(2002)
    source = SourceService(db).create(user, {
        "type": "telegram_chat", "name": "Project chat", "external_source_id": "1001",
    })
    project = ProjectService(db).create(user, {"name": "Alpha"})
    foreign = ProjectService(db).create(other, {"name": "Private"})
    url = f"/api/v1/sources/{source.id}/projects"
    assert client.put(url, headers=headers, json={
        "project_ids": [str(foreign.id)],
    }).status_code == 404
    assert client.put(url, headers=headers, json={
        "project_ids": [str(project.id), str(project.id)],
    }).status_code == 200
    assert [p["id"] for p in client.get(url, headers=headers).json()["data"]] == [str(project.id)]
    ProjectService(db).patch(user, project.id, {"is_archived": True})
    assert client.get(url, headers=headers).json()["data"] == []
    assert client.put(url, headers=headers, json={
        "project_ids": [str(project.id)],
    }).status_code == 422


def test_source_project_mapping_auto_and_ambiguous_choice(client, db, monkeypatch):
    user = UserService(db).get_or_create(9555)
    source = SourceService(db).create(user, {
        "type": "telegram_chat", "name": "Chat", "external_source_id": "9555",
    })
    message = Message(
        user_id=user.id, source_id=source.id, external_message_id="project-1",
        message_type="text", text="Задача", received_at=datetime.now(UTC),
    )
    db.add(message)
    db.commit()
    result = AIResult(
        intent="create_task", confidence=0.9, entities={"title": "Task"},
        action=AIAction(type="create_task"), reason="test",
    )
    service = AIActionService(db)
    assert service.apply(user, result, message)["entities"]["project_id"] is None
    first = ProjectService(db).create(user, {"name": "Alpha"})
    second = ProjectService(db).create(user, {"name": "Beta"})
    SourceService(db).replace_projects(user, source.id, [first.id])
    assert service.apply(user, result, message)["entities"]["project_id"] == str(first.id)
    SourceService(db).replace_projects(user, source.id, [first.id, second.id])
    outcome = service.apply(user, result, message)
    assert outcome["state"] == "project_choice"
    _, markup = outcome_message(db, user, outcome)
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        "Alpha", "Beta", "Другой", "Inbox",
    ]

    monkeypatch.setattr(TelegramClient, "answer_callback", Mock())
    monkeypatch.setattr(TelegramClient, "edit_message", Mock())
    token = markup["inline_keyboard"][1][0]["callback_data"]
    callback = {"update_id": 9556, "callback_query": {
        "id": "choose", "from": {"id": 9555}, "data": token,
        "message": {"message_id": 21, "chat": {"id": 9555, "type": "private"}},
    }}
    response = client.post(
        "/webhooks/telegram", json=callback,
        headers={"X-Telegram-Bot-Api-Secret-Token": "change-me"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "select_project"
    proposal = db.scalar(select(UIAction).where(
        UIAction.user_id == user.id, UIAction.action == "confirm_ai",
    ).order_by(UIAction.id.desc()))
    assert proposal.payload["result"]["entities"]["project_id"] == str(second.id)
    executed = service.execute(
        user, AIResult.model_validate(proposal.payload["result"]), message,
    )
    assert db.get(Task, uuid.UUID(executed["object_id"])).project_id == second.id


def test_source_project_bot_toggle(db):
    user = UserService(db).get_or_create(9557)
    source = SourceService(db).create(user, {
        "type": "telegram_chat", "name": "Chat", "external_source_id": "9557",
    })
    project = ProjectService(db).create(user, {"name": "Alpha"})
    _, source_markup = object_view(db, user, "sources", source.id)
    view_token = source_markup["inline_keyboard"][0][0]["callback_data"]
    text, markup = handle_action(db, user, UIActionService(db).consume(user, view_token))
    assert "связаны" in text
    toggle_token = markup["inline_keyboard"][0][0]["callback_data"]
    text, markup = handle_action(db, user, UIActionService(db).consume(user, toggle_token))
    assert markup["inline_keyboard"][0][0]["text"].startswith("☑")
    assert [p.id for p in SourceService(db).projects(user, source.id)] == [project.id]
