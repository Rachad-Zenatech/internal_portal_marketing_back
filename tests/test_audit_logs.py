"""Audit log readers: permission code and readable names for initiatives."""
import uuid

import pytest

from tests.conftest import run_sql


@pytest.fixture
def auditor(viewer):
    """The temporary regular user, given only the "View Audit Logs" permission group."""
    code = f"PYTEST_AUDITOR_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql(
        "INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Auditor', true) RETURNING id", code
    )[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'LOGS_VIEW_AUDIT'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    try:
        yield viewer
    finally:
        run_sql("DELETE FROM user_roles WHERE role_id = $1", role_id)
        run_sql("DELETE FROM roles WHERE id = $1", role_id)


@pytest.fixture
def audited_initiative(client, admin, ref):
    name = f"Pytest Audit Expo {uuid.uuid4().hex[:6]}"
    response = client.post(
        "/api/initiatives",
        json={"name": name, "brand_id": ref["brands"]["DESKFLEX"], "channel_id": ref["channels"]["EVENTS"]},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_audit_log_viewer_can_read_system_logs(client, auditor):
    # Previously checked AUDIT_LOGS_VIEW, a permission that doesn't exist, so only super admins got in
    assert client.get("/api/logs", headers=auditor["headers"]).status_code == 200
    assert client.get("/api/audit-logs", headers=auditor["headers"]).status_code == 200


def test_user_without_audit_permission_is_refused(client, viewer):
    assert client.get("/api/logs", headers=viewer["headers"]).status_code == 403


def test_system_logs_show_and_search_initiative_names(client, admin, audited_initiative):
    response = client.get(
        "/api/logs", params={"search": audited_initiative["name"]}, headers=admin["headers"]
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [(i["entity_type"], i["entity_title"]) for i in items] == [("Initiative", audited_initiative["name"])]


def test_audit_log_shows_initiative_names(client, admin, audited_initiative):
    response = client.get("/api/audit-logs", headers=admin["headers"])

    assert response.status_code == 200
    entry = next(e for e in response.json() if e["entity_id"] == audited_initiative["id"])
    assert entry["entity_title"] == audited_initiative["name"]


def test_removed_initiative_keeps_its_name_in_the_log(client, admin, audited_initiative):
    run_sql("DELETE FROM initiatives WHERE id = $1", audited_initiative["id"])

    response = client.get(
        "/api/logs", params={"entity_type": "Initiative", "entity_id": audited_initiative["id"]}, headers=admin["headers"]
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["entity_title"] == audited_initiative["name"]
