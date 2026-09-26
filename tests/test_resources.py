from datetime import UTC, datetime, timedelta

from app.models.entities import CalendarConnection, InboxItem, Message, Source, SourceFolder
from app.services.domain import UserService


def unwrap(response, status=200):
    assert response.status_code == status, response.text
    return response.json()["data"]


def test_source_disconnect_reconnect_and_settings(client, db, headers):
    source = unwrap(
        client.post(
            "/api/v1/sources",
            headers=headers,
            json={"type": "telegram_channel", "name": "News", "external_source_id": "-10"},
        ),
        201,
    )
    updated = unwrap(
        client.patch(
            f"/api/v1/sources/{source['id']}",
            headers=headers,
            json={"analysis_text": False, "status": "paused"},
        )
    )
    assert not updated["analysis_text"]
    disconnected = unwrap(client.delete(f"/api/v1/sources/{source['id']}", headers=headers))
    assert disconnected["status"] == "disconnected"
    assert (
        client.patch(
            f"/api/v1/sources/{source['id']}", headers=headers, json={"name": "No"}
        ).status_code
        == 409
    )
    reconnected = unwrap(
        client.post(
            "/api/v1/sources",
            headers=headers,
            json={"type": "telegram_channel", "name": "News 2", "external_source_id": "-10"},
        ),
        201,
    )
    assert reconnected["id"] == source["id"] and reconnected["status"] == "active"
    assert (
        client.post(
            "/api/v1/sources",
            headers=headers,
            json={"type": "telegram_channel", "name": "Dup", "external_source_id": "-10"},
        ).status_code
        == 409
    )


def test_source_folders(client, db, headers):
    user = UserService(db).get_or_create(1001)
    source = Source(
        user_id=user.id,
        type="gmail",
        name="Mail",
        status="active",
        external_source_id="mail@example",
    )
    db.add(source)
    db.flush()
    db.add_all(
        [
            SourceFolder(source_id=source.id, external_folder_id="INBOX", name="Inbox"),
            SourceFolder(source_id=source.id, external_folder_id="SPAM", name="Spam"),
        ]
    )
    db.commit()
    assert len(unwrap(client.get(f"/api/v1/sources/{source.id}/folders", headers=headers))) == 2
    folders = unwrap(
        client.patch(
            f"/api/v1/sources/{source.id}/folders",
            headers=headers,
            json={"folders": [{"external_folder_id": "SPAM", "is_selected": False}]},
        )
    )
    assert any(x["external_folder_id"] == "SPAM" and not x["is_selected"] for x in folders)
    assert (
        client.patch(
            f"/api/v1/sources/{source.id}/folders",
            headers=headers,
            json={"folders": [{"external_folder_id": "INBOX", "is_selected": False}]},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/sources/{source.id}/folders",
            headers=headers,
            json={"folders": [{"external_folder_id": "UNKNOWN", "is_selected": True}]},
        ).status_code
        == 422
    )


def test_inbox_transitions_and_reply_policy(client, db, headers):
    user = UserService(db).get_or_create(1001)
    source = Source(
        user_id=user.id, type="gmail", name="Mail", status="active", external_source_id="mail"
    )
    db.add(source)
    db.flush()
    email = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="e1",
        message_type="email",
        sender_email="sender@example.com",
        text="Hello",
        received_at=datetime.now(UTC),
    )
    telegram = Message(
        user_id=user.id,
        source_id=source.id,
        external_message_id="t1",
        message_type="text",
        text="Hi",
        received_at=datetime.now(UTC),
    )
    db.add_all([email, telegram])
    db.flush()
    first = InboxItem(
        user_id=user.id, message_id=email.id, item_type="reply_required", title="Reply"
    )
    second = InboxItem(
        user_id=user.id, message_id=telegram.id, item_type="question", title="Question"
    )
    db.add_all([first, second])
    db.commit()
    assert (
        unwrap(client.post(f"/api/v1/inbox/{first.id}/resolve", headers=headers))["status"]
        == "resolved"
    )
    assert (
        unwrap(client.post(f"/api/v1/inbox/{first.id}/ignore", headers=headers))["status"]
        == "ignored"
    )
    until = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert (
        unwrap(
            client.post(f"/api/v1/inbox/{first.id}/snooze", headers=headers, json={"until": until})
        )["status"]
        == "snoozed"
    )
    token = unwrap(
        client.post(
            f"/api/v1/inbox/{first.id}/reply", headers=headers, json={"draft_text": "Thanks"}
        )
    )["confirmation_token"]
    assert token.startswith("a:")
    assert (
        client.post(
            f"/api/v1/inbox/{second.id}/reply", headers=headers, json={"draft_text": "No"}
        ).status_code
        == 409
    )


def test_waiting_and_reminders(client, db, headers):
    waiting = unwrap(
        client.post("/api/v1/waiting-for", headers=headers, json={"title": "Reply from Alice"}), 201
    )
    assert (
        unwrap(
            client.patch(
                f"/api/v1/waiting-for/{waiting['id']}",
                headers=headers,
                json={"expected_from": "Alice"},
            )
        )["expected_from"]
        == "Alice"
    )
    assert (
        unwrap(client.post(f"/api/v1/waiting-for/{waiting['id']}/complete", headers=headers))[
            "status"
        ]
        == "completed"
    )
    second = unwrap(
        client.post("/api/v1/waiting-for", headers=headers, json={"title": "Other"}), 201
    )
    assert (
        unwrap(client.post(f"/api/v1/waiting-for/{second['id']}/cancel", headers=headers))["status"]
        == "cancelled"
    )
    due = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    reminder = unwrap(
        client.post("/api/v1/reminders", headers=headers, json={"title": "Ping", "due_at": due}),
        201,
    )
    assert (
        unwrap(
            client.patch(
                f"/api/v1/reminders/{reminder['id']}", headers=headers, json={"title": "Ping now"}
            )
        )["title"]
        == "Ping now"
    )
    from app.services.domain import ReminderService

    assert ReminderService(db).fire_due() == 1
    notes = unwrap(client.get("/api/v1/notifications", headers=headers))
    assert notes[0]["type"] == "task_reminder"
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    pending = unwrap(
        client.post(
            "/api/v1/reminders", headers=headers, json={"title": "Later", "due_at": future}
        ),
        201,
    )
    assert (
        unwrap(client.post(f"/api/v1/reminders/{pending['id']}/cancel", headers=headers))["status"]
        == "cancelled"
    )
    assert (
        client.post(f"/api/v1/reminders/{pending['id']}/cancel", headers=headers).status_code == 409
    )


def test_calendar_crud_disconnect(client, db, headers):
    user = UserService(db).get_or_create(1001)
    connection = CalendarConnection(
        user_id=user.id, provider="google", external_account_id="a@b", status="active"
    )
    db.add(connection)
    db.commit()
    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)
    event = unwrap(
        client.post(
            "/api/v1/calendar/events",
            headers=headers,
            json={
                "connection_id": str(connection.id),
                "title": "Meeting",
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
            },
        ),
        201,
    )
    assert (
        unwrap(
            client.patch(
                f"/api/v1/calendar/events/{event['id']}", headers=headers, json={"title": "Sync"}
            )
        )["title"]
        == "Sync"
    )
    assert (
        unwrap(client.post(f"/api/v1/calendar/events/{event['id']}/cancel", headers=headers))[
            "status"
        ]
        == "cancelled"
    )
    assert len(unwrap(client.get("/api/v1/calendar/connections", headers=headers))) == 1
    assert (
        unwrap(client.delete(f"/api/v1/calendar/connections/{connection.id}", headers=headers))[
            "status"
        ]
        == "disconnected"
    )
    assert (
        client.post(
            "/api/v1/calendar/events",
            headers=headers,
            json={
                "connection_id": str(connection.id),
                "title": "No",
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
            },
        ).status_code
        == 409
    )
