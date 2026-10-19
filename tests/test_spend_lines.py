"""Costs on an initiative: /api/initiatives/{id}/spend-lines and /api/spend-lines/{line_id}."""
import json
import uuid
from datetime import date

import pytest

from tests.conftest import run_sql


@pytest.fixture(scope="module")
def categories():
    """Spend category ids by code."""
    return {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM spend_categories")}


@pytest.fixture
def event(client, admin, ref):
    """An in-person event in GBP (so currency defaults are visible)."""
    response = client.post(
        "/api/initiatives",
        json={
            "name": f"Pytest Cost Expo {uuid.uuid4().hex[:6]}",
            "brand_id": ref["brands"]["DESKFLEX"],
            "channel_id": ref["channels"]["EVENTS"],
            "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
            "currency": "GBP",
            "event": {"city": "London", "country_code": "GB"},
        },
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user, given only the "View Initiatives" permission group.

    The role (code PYTEST_…) is removed with the user by _delete_user in conftest.
    """
    code = f"PYTEST_INITIATIVE_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql(
        "INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Initiative Viewer', true) RETURNING id", code
    )[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    return viewer


def _add(client, user, initiative_id, **fields):
    body = {"amount": 600, "incurred_on": "2026-11-04", **fields}
    return client.post(f"/api/initiatives/{initiative_id}/spend-lines", json=body, headers=user["headers"])


def _patch(client, user, line_id, body):
    return client.patch(f"/api/spend-lines/{line_id}", json=body, headers=user["headers"])


def _audits(line_id):
    return run_sql("SELECT action, old_value, new_value FROM audit_logs WHERE entity_type = 'SpendLine' AND entity_id = $1 ORDER BY created_at", str(line_id))


def _insert_line(initiative_id, category_id, **columns):
    """A line written straight to the database, as a feed or the reconciliation would."""
    values = {"amount": 250, "currency": "GBP", "incurred_on": date(2026, 11, 4), **columns}
    names = ", ".join(values)
    params = ", ".join(f"${n}" for n in range(3, len(values) + 3))
    return run_sql(
        f"INSERT INTO spend_lines (initiative_id, category_id, {names}) VALUES ($1, $2, {params}) RETURNING id",
        initiative_id, category_id, *values.values(),
    )[0]["id"]


def test_crud_happy_path(client, admin, event, categories):
    created = _add(client, admin, event["id"], category_id=categories["FLOORING"], vendor="Freeman",
                   description="10x20 grey carpet", invoice_ref="  INV-77  ")
    assert created.status_code == 201, created.text
    line = created.json()
    assert (line["category_code"], line["expense_type_code"], line["invoice_ref"], line["editable"]) == (
        "FLOORING", "SHOW_SERVICES", "INV-77", True,
    )

    listed = client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"])
    assert [item["id"] for item in listed.json()] == [line["id"]]

    updated = _patch(client, admin, line["id"], {"amount": 650, "description": "10x20 blue carpet"})
    assert updated.status_code == 200, updated.text
    assert (updated.json()["amount"], updated.json()["description"]) == (650, "10x20 blue carpet")

    deleted = client.delete(f"/api/spend-lines/{line['id']}", headers=admin["headers"])
    assert deleted.status_code == 204
    assert client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"]).json() == []

    audits = _audits(line["id"])
    assert [a["action"] for a in audits] == ["CREATE", "UPDATE", "DELETE"]
    update = audits[1]
    assert json.loads(update["old_value"]) == {"initiative_id": event["id"], "amount": 600.0, "description": "10x20 grey carpet"}
    assert json.loads(update["new_value"]) == {"initiative_id": event["id"], "amount": 650.0, "description": "10x20 blue carpet"}
    assert json.loads(audits[2]["old_value"])["initiative_id"] == event["id"]


def test_lines_are_ordered_by_expense_type_category_and_date(client, admin, event, categories):
    for code, day in [("FREIGHT", "2026-10-02"), ("BOOTH_SPACE", "2026-06-01"), ("LIGHTING", "2026-11-04"), ("FLOORING", "2026-11-03")]:
        assert _add(client, admin, event["id"], category_id=categories[code], incurred_on=day).status_code == 201

    listed = client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"]).json()

    assert [item["category_code"] for item in listed] == ["BOOTH_SPACE", "LIGHTING", "FLOORING", "FREIGHT"]


def test_currency_and_cost_type_defaults(client, admin, event, categories):
    build = _add(client, admin, event["id"], category_id=categories["BOOTH_BUILD"]).json()
    rented = _add(client, admin, event["id"], category_id=categories["BOOTH_BUILD"], cost_type="OPEX", currency="usd").json()

    assert (build["currency"], build["cost_type"]) == ("GBP", "CAPEX")  # the initiative's currency, the category's cost type
    assert (rented["currency"], rented["cost_type"]) == ("USD", "OPEX")


def test_estimate_is_copied_and_variance_shows_once_invoiced(client, admin, event, categories):
    line = _add(client, admin, event["id"], category_id=categories["FREIGHT"], status="ESTIMATED", amount=1800).json()
    assert (line["estimated_amount"], line["variance"]) == (1800, None)

    invoiced = _patch(client, admin, line["id"], {"status": "INVOICED", "amount": 2100, "invoice_ref": "FR-9"})

    assert invoiced.status_code == 200, invoiced.text
    assert (invoiced.json()["estimated_amount"], invoiced.json()["amount"], invoiced.json()["variance"]) == (1800, 2100, 300)


def test_paid_needs_a_paid_date(client, admin, event, categories):
    created = _add(client, admin, event["id"], category_id=categories["BADGES_PASSES"], status="PAID")
    line = _add(client, admin, event["id"], category_id=categories["BADGES_PASSES"]).json()
    patched = _patch(client, admin, line["id"], {"status": "PAID"})

    for response in (created, patched):
        assert response.status_code == 422
        assert response.json()["detail"] == "Add the date it was paid."
    assert _patch(client, admin, line["id"], {"status": "PAID", "paid_on": "2026-11-10"}).status_code == 200


def test_travel_can_be_entered_by_hand(client, admin, event, categories):
    """Navan adds its own bookings; per diems, Ubers and anything booked elsewhere are entered here."""
    for code in ("PER_DIEMS_MEALS", "GROUND_TRANSPORT", "FLIGHTS"):
        response = _add(client, admin, event["id"], category_id=categories[code])
        assert response.status_code == 201, response.text


def test_ads_category_rejected_on_an_event(client, admin, event, categories):
    response = _add(client, admin, event["id"], category_id=categories["MEDIA_ADS"])

    assert response.status_code == 422
    assert response.json()["detail"] == "That category isn't used for this channel."


def test_duplicate_supplier_invoice(client, admin, event, categories):
    first = _add(client, admin, event["id"], category_id=categories["FLOORING"], vendor="Freeman", invoice_ref="INV-2231")
    assert first.status_code == 201, first.text

    duplicate = _add(client, admin, event["id"], category_id=categories["LIGHTING"], vendor="FREEMAN ", invoice_ref="INV-2231")
    other_vendor = _add(client, admin, event["id"], category_id=categories["LIGHTING"], vendor="GES", invoice_ref="INV-2231")

    assert duplicate.status_code == 422
    assert duplicate.json()["detail"] == f"Invoice INV-2231 from Freeman is already recorded on {event['event_code']} · {event['name']}."
    assert other_vendor.status_code == 201, other_vendor.text
    # The same check applies when an edit would create the duplicate
    clash = _patch(client, admin, other_vendor.json()["id"], {"vendor": "freeman"})
    assert clash.status_code == 422
    assert "is already recorded on" in clash.json()["detail"]


def test_navan_lines_are_read_only(client, admin, event, categories):
    line_id = _insert_line(event["id"], categories["OTHER_TRAVEL"], source="NAVAN", external_ref=f"navan:pytest:{uuid.uuid4()}")

    listed = client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"]).json()
    edited = _patch(client, admin, line_id, {"note": "changed"})
    deleted = client.delete(f"/api/spend-lines/{line_id}", headers=admin["headers"])

    assert listed[0]["editable"] is False
    message = "This cost is the total of the event's Navan bookings and updates automatically."
    assert (edited.status_code, edited.json()["detail"]) == (422, message)
    assert (deleted.status_code, deleted.json()["detail"]) == (422, message)


def test_leftover_travel_feed_lines_can_be_edited_and_deleted(client, admin, event, categories):
    """The old travel feed no longer updates its lines, so they can be corrected or removed like manual ones."""
    line_id = _insert_line(event["id"], categories["FLIGHTS"], source="TRAVEL_FEED", external_ref="TRIP-1")

    [listed] = client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"]).json()
    edited = _patch(client, admin, line_id, {"note": "changed", "amount": 300})

    assert (listed["lock_reason"], listed["editable"], listed["can_delete"]) == (None, True, True)
    assert edited.status_code == 200, edited.text
    assert (edited.json()["note"], edited.json()["amount"]) == ("changed", 300)
    assert client.delete(f"/api/spend-lines/{line_id}", headers=admin["headers"]).status_code == 204
    assert client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"]).json() == []


def test_reconciled_lines_keep_their_figures(client, admin, event, categories):
    line_id = _insert_line(event["id"], categories["FLOORING"], status="INVOICED", reconciled_month=date(2026, 10, 1))
    message = "This cost was reconciled for October 2026. Ask Finance to reopen it."

    amount = _patch(client, admin, line_id, {"amount": 300})
    note = _patch(client, admin, line_id, {"note": "Credit note expected", "invoice_ref": "INV-5"})
    deleted = client.delete(f"/api/spend-lines/{line_id}", headers=admin["headers"])

    assert (amount.status_code, amount.json()["detail"]) == (422, message)
    assert note.status_code == 200, note.text
    assert (note.json()["note"], note.json()["amount"]) == ("Credit note expected", 250)
    assert (deleted.status_code, deleted.json()["detail"]) == (422, message)


def test_viewer_can_list_but_not_change(client, admin, initiative_viewer, event, categories):
    line = _add(client, admin, event["id"], category_id=categories["FLOORING"]).json()

    assert client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=initiative_viewer["headers"]).status_code == 200
    assert _add(client, initiative_viewer, event["id"], category_id=categories["FLOORING"]).status_code == 403
    assert _patch(client, initiative_viewer, line["id"], {"amount": 1}).status_code == 403
    assert client.delete(f"/api/spend-lines/{line['id']}", headers=initiative_viewer["headers"]).status_code == 403


def test_missing_items_return_404(client, admin, categories):
    missing = uuid.uuid4()

    assert client.get(f"/api/initiatives/{missing}/spend-lines", headers=admin["headers"]).status_code == 404
    assert _add(client, admin, missing, category_id=categories["FLOORING"]).status_code == 404
    assert _patch(client, admin, missing, {"amount": 1}).status_code == 404
    assert client.delete(f"/api/spend-lines/{missing}", headers=admin["headers"]).status_code == 404


def test_deleting_the_initiative_removes_its_lines(client, admin, event, categories):
    line = _add(client, admin, event["id"], category_id=categories["FLOORING"]).json()

    run_sql("DELETE FROM initiatives WHERE id = $1", event["id"])

    assert run_sql("SELECT 1 FROM spend_lines WHERE id = $1", line["id"]) == []


def test_audit_log_shows_a_readable_name(client, admin, event, categories):
    line = _add(client, admin, event["id"], category_id=categories["BOOTH_SPACE"]).json()
    client.delete(f"/api/spend-lines/{line['id']}", headers=admin["headers"])

    response = client.get("/api/logs", params={"entity_type": "SpendLine", "entity_id": line["id"]}, headers=admin["headers"])

    assert response.status_code == 200
    titles = {entry["entity_title"] for entry in response.json()["items"]}
    assert titles == {f"Booth space · {event['event_code']}"}  # still readable after the cost is deleted


RECONCILED = "This cost was reconciled for October 2026. Ask Finance to reopen it."


def _reconciled(event, categories, status="INVOICED", **columns):
    if status == "PAID":
        columns.setdefault("paid_on", date(2026, 10, 20))
    return _insert_line(
        event["id"], categories["BOOTH_BUILD"], status=status, estimated_amount=200, cost_type="CAPEX",
        reconciled_month=date(2026, 10, 1), **columns,
    )


def test_reconciled_invoiced_line_can_be_paid(client, admin, event, categories):
    line_id = _reconciled(event, categories)

    without_date = _patch(client, admin, line_id, {"status": "PAID"})
    paid = _patch(client, admin, line_id, {"status": "PAID", "paid_on": "2026-10-20", "payment_method": "AP_INVOICE"})

    assert (without_date.status_code, without_date.json()["detail"]) == (422, "Add the date it was paid.")
    assert paid.status_code == 200, paid.text
    assert (paid.json()["status"], paid.json()["paid_on"], paid.json()["payment_method"]) == ("PAID", "2026-10-20", "AP_INVOICE")


def test_reconciled_paid_line_keeps_its_payment_details(client, admin, event, categories):
    line_id = _reconciled(event, categories, status="PAID")

    for change in ({"paid_on": "2026-10-25"}, {"payment_method": "CORPORATE_CARD"}):
        response = _patch(client, admin, line_id, change)
        assert (response.status_code, response.json()["detail"]) == (422, RECONCILED)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"estimated_amount": 300}, RECONCILED),
        ({"cost_type": "OPEX"}, RECONCILED),
        ({"status": "COMMITTED"}, "A reconciled cost can only move from Invoiced to Paid."),
        ({"status": "ESTIMATED"}, "A reconciled cost can only move from Invoiced to Paid."),
    ],
)
def test_reconciled_line_rejects_figure_and_status_changes(client, admin, event, categories, change, message):
    line_id = _reconciled(event, categories)

    response = _patch(client, admin, line_id, change)

    assert (response.status_code, response.json()["detail"]) == (422, message)


def test_reconciled_paid_line_cannot_move_back(client, admin, event, categories):
    line_id = _reconciled(event, categories, status="PAID")

    response = _patch(client, admin, line_id, {"status": "INVOICED"})

    assert (response.status_code, response.json()["detail"]) == (422, "A reconciled cost can only move from Invoiced to Paid.")


def test_reconciled_line_text_fields_stay_editable(client, admin, event, categories):
    line_id = _reconciled(event, categories, status="PAID", vendor="Freeman")
    other = _add(client, admin, event["id"], category_id=categories["FLOORING"], vendor="Freeman", invoice_ref="INV-9")
    assert other.status_code == 201, other.text

    edited = _patch(client, admin, line_id, {"note": "Matched to PO 77", "invoice_ref": "INV-8", "description": "Booth build"})
    duplicate = _patch(client, admin, line_id, {"invoice_ref": "INV-9"})

    assert edited.status_code == 200, edited.text
    assert (edited.json()["note"], edited.json()["invoice_ref"]) == ("Matched to PO 77", "INV-8")
    # The vendor + invoice duplicate check still applies to reconciled lines
    assert duplicate.status_code == 422
    assert "is already recorded on" in duplicate.json()["detail"]


def test_lock_information_in_responses(client, admin, event, categories):
    manual = _add(client, admin, event["id"], category_id=categories["FLOORING"]).json()
    travel_id = _insert_line(event["id"], categories["OTHER_TRAVEL"], source="NAVAN", external_ref=f"navan:pytest:{uuid.uuid4()}")
    invoiced_id = _reconciled(event, categories)
    paid_id = _reconciled(event, categories, status="PAID")

    lines = {line["id"]: line for line in client.get(f"/api/initiatives/{event['id']}/spend-lines", headers=admin["headers"]).json()}
    state = {key: (lines[str(key)]["lock_reason"], lines[str(key)]["can_delete"], lines[str(key)]["editable"]) for key in lines}

    assert state[manual["id"]] == (None, True, True)
    assert lines[manual["id"]]["locked_fields"] == []
    assert state[str(travel_id)] == ("NAVAN", False, False)
    assert len(lines[str(travel_id)]["locked_fields"]) == 13  # every editable field
    assert state[str(invoiced_id)] == ("RECONCILED", False, True)
    assert lines[str(invoiced_id)]["locked_fields"] == ["category_id", "estimated_amount", "amount", "currency", "incurred_on", "cost_type"]
    assert lines[str(paid_id)]["locked_fields"] == [
        "category_id", "status", "estimated_amount", "amount", "currency", "incurred_on", "paid_on", "payment_method", "cost_type",
    ]


def test_database_rejects_reconciled_line_that_is_not_invoiced(event, categories):
    with pytest.raises(Exception, match="spend_lines_reconciled_status_check"):
        _insert_line(event["id"], categories["FLOORING"], status="COMMITTED", reconciled_month=date(2026, 10, 1))


def test_delete_user_removes_temporary_roles(initiative_viewer):
    from tests.conftest import _delete_user

    role_ids = [r["role_id"] for r in run_sql(
        "SELECT ur.role_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = $1 AND r.code LIKE 'PYTEST\\_%'",
        initiative_viewer["id"],
    )]
    assert role_ids

    _delete_user(initiative_viewer["id"])

    assert run_sql("SELECT 1 FROM roles WHERE id = ANY($1::uuid[])", role_ids) == []
    assert run_sql("SELECT 1 FROM role_permission_groups WHERE role_id = ANY($1::uuid[])", role_ids) == []
    # the shared USER role is untouched
    assert run_sql("SELECT 1 FROM roles WHERE code = 'USER'") != []
