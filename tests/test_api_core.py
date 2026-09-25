from datetime import UTC, datetime, timedelta


def data(response, status=200):
    assert response.status_code == status, response.text
    return response.json()["data"]


def test_health_auth_and_profile(client, headers):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/v1/me").status_code == 401
    me = data(client.get("/api/v1/me", headers=headers))
    assert me["telegram_user_id"] == 1001
    patched = data(
        client.patch(
            "/api/v1/me", headers=headers, json={"timezone": "Europe/Moscow", "locale": "ru"}
        )
    )
    assert patched["timezone"] == "Europe/Moscow"
    assert (
        client.get("/api/v1/me", headers={**headers, "Authorization": "Bearer wrong"}).json()[
            "error"
        ]["code"]
        == "AUTH_ERROR"
    )


def test_projects_crud_conflict_and_ownership(client, headers, other_headers):
    project = data(
        client.post("/api/v1/projects", headers=headers, json={"name": "Work", "description": "A"}),
        201,
    )
    assert data(client.get("/api/v1/projects", headers=headers))[0]["name"] == "Work"
    assert (
        client.post("/api/v1/projects", headers=headers, json={"name": "Work"}).status_code == 409
    )
    updated = data(
        client.patch(f"/api/v1/projects/{project['id']}", headers=headers, json={"name": "Job"})
    )
    assert updated["name"] == "Job"
    assert client.get(f"/api/v1/projects/{project['id']}", headers=other_headers).status_code == 404
    assert client.delete(f"/api/v1/projects/{project['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/v1/projects/{project['id']}", headers=headers).status_code == 404


def test_task_lifecycle_filters_and_undo(client, headers):
    project = data(client.post("/api/v1/projects", headers=headers, json={"name": "Home"}), 201)
    due = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Buy milk", "project_id": project["id"], "priority": "high", "due_at": due},
    )
    task = data(response, 201)
    create_undo = response.json()["meta"]["undo_token"]
    assert data(client.get(f"/api/v1/tasks/{task['id']}", headers=headers))["title"] == "Buy milk"
    listing = client.get("/api/v1/tasks?priority=high&search=milk", headers=headers).json()
    assert listing["meta"]["pagination"]["total"] == 1
    changed_response = client.patch(
        f"/api/v1/tasks/{task['id']}", headers=headers, json={"title": "Buy oat milk"}
    )
    assert data(changed_response)["title"] == "Buy oat milk"
    patch_undo = changed_response.json()["meta"]["undo_token"]
    assert (
        data(
            client.post(
                f"/api/v1/tasks/{task['id']}/undo", headers=headers, params={"token": patch_undo}
            )
        )["title"]
        == "Buy milk"
    )
    assert (
        client.post(
            f"/api/v1/tasks/{task['id']}/undo", headers=headers, params={"token": patch_undo}
        ).status_code
        == 409
    )
    complete = client.post(f"/api/v1/tasks/{task['id']}/complete", headers=headers)
    assert data(complete)["status"] == "completed"
    assert (
        data(client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=headers))["status"]
        == "cancelled"
    )
    deleted = client.delete(f"/api/v1/tasks/{task['id']}", headers=headers)
    delete_undo = deleted.json()["meta"]["undo_token"]
    assert client.get(f"/api/v1/tasks/{task['id']}", headers=headers).status_code == 404
    assert (
        data(
            client.post(
                f"/api/v1/tasks/{task['id']}/undo", headers=headers, params={"token": delete_undo}
            )
        )["deleted_at"]
        is None
    )
    # Undoing a creation hides it with a soft delete.
    assert (
        client.post(
            f"/api/v1/tasks/{task['id']}/undo", headers=headers, params={"token": create_undo}
        ).status_code
        == 200
    )


def test_task_validation_project_and_not_found(client, headers):
    assert client.post("/api/v1/tasks", headers=headers, json={"title": ""}).status_code == 422
    fake = "00000000-0000-0000-0000-000000000001"
    assert (
        client.post(
            "/api/v1/tasks", headers=headers, json={"title": "x", "project_id": fake}
        ).status_code
        == 404
    )
    assert client.get(f"/api/v1/tasks/{fake}", headers=headers).status_code == 404


def test_today_stats_and_analyze_stub(client, headers):
    due = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    data(client.post("/api/v1/tasks", headers=headers, json={"title": "Today", "due_at": due}), 201)
    assert len(data(client.get("/api/v1/today", headers=headers))["tasks"]) == 1
    assert data(client.get("/api/v1/stats", headers=headers))["tasks"] == 1
    stub = data(client.post("/api/v1/analyze", headers=headers, json={}), 202)
    assert stub["status"] == "no_sources"
