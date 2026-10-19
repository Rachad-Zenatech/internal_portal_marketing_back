"""Monthly budgets: markets, /api/budgets, money moves (/api/budget-transfers) and comments (/api/budget-comments).

Budgets here use months far in the past (finished, so Complete) or the future (Not started), which no real
data uses; everything in them is removed after each test.
"""
import json
import uuid
from datetime import date, timedelta

import pytest

from services.audit_service import entity_title_sql
from tests.conftest import run_sql

MARKETS = ["LONDON", "NORTH_AMERICA", "GERMANY", "AUSTRALIA", "KOREA", "UNASSIGNED"]

# Test months: before 2002 or from 2090 on
_TEST_MONTHS = "month < DATE '2002-01-01' OR month >= DATE '2090-01-01'"


@pytest.fixture(autouse=True)
def _clean_budgets():
    yield
    run_sql(
        f"DELETE FROM budget_transfers WHERE from_budget_id IN (SELECT id FROM monthly_budgets WHERE {_TEST_MONTHS}) "
        f"OR to_budget_id IN (SELECT id FROM monthly_budgets WHERE {_TEST_MONTHS})"
    )
    run_sql(f"DELETE FROM budget_comments WHERE {_TEST_MONTHS}")
    run_sql(f"DELETE FROM monthly_budgets WHERE {_TEST_MONTHS}")


@pytest.fixture(scope="module")
def markets():
    return {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM markets")}


@pytest.fixture(scope="module")
def category_id():
    return run_sql("SELECT id FROM spend_categories WHERE code = 'OTHER'")[0]["id"]


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user, given only the "View Initiatives" permission group."""
    code = f"PYTEST_INITIATIVE_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql("INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Initiative Viewer', true) RETURNING id", code)[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    return viewer


def _initiative(client, admin, ref, brand, channel, market_id=None):
    body = {
        "name": f"Pytest Budget {brand} {channel} {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"][brand],
        "channel_id": ref["channels"][channel],
        "sub_channel_id": ref["sub_channels"]["OTHER"],
        "currency": "USD",
        "budget_amount": 0,
    }
    if market_id is not None:
        body["market_id"] = market_id
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def _line(initiative, category_id, amount, incurred_on, status="COMMITTED", currency="USD"):
    """A cost written straight to the database (the cost rules are tested elsewhere)."""
    run_sql(
        "INSERT INTO spend_lines (initiative_id, category_id, amount, currency, incurred_on, status, paid_on) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        uuid.UUID(initiative["id"]), category_id, amount, currency, incurred_on, status,
        incurred_on if status == "PAID" else None,
    )


def _budget(client, user, month, market_id, amount=1000, currency="USD", brand_id=None, channel_id=None, **extra):
    body = {"month": month, "brand_id": brand_id, "market_id": market_id, "channel_id": channel_id,
            "amount": amount, "currency": currency, **extra}
    return client.post("/api/budgets", json=body, headers=user["headers"])


def _make_budget(client, user, month, market_id, **fields):
    response = _budget(client, user, month, market_id, **fields)
    assert response.status_code == 201, response.text
    return response.json()


def _month(client, user, month):
    response = client.get("/api/budgets", params={"month": month}, headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(month_view):
    return {b["id"]: b for b in month_view["budgets"]}


def _audit_titles(entity_type, entity_id):
    return [
        r["title"]
        for r in run_sql(
            f"SELECT {entity_title_sql('al')} AS title FROM audit_logs al "
            "WHERE al.entity_type = $1 AND al.entity_id = $2 ORDER BY al.created_at",
            entity_type, str(entity_id),
        )
    ]


# --- Markets ---


def test_reference_data_includes_markets(client, viewer):
    response = client.get("/api/reference-data", headers=viewer["headers"])

    assert response.status_code == 200
    items = response.json()["markets"]
    assert [m["code"] for m in items] == MARKETS  # in display order, Unassigned last
    assert {m["code"]: m["name"] for m in items}["NORTH_AMERICA"] == "North America"
    assert all(set(m) == {"id", "code", "name", "sort_order"} for m in items)


def test_initiative_market(client, admin, ref, markets):
    created = _initiative(client, admin, ref, "OTHERSHIP", "SEO", markets["LONDON"])
    assert (created["market_id"], created["market_name"]) == (markets["LONDON"], "London")
    no_market = _initiative(client, admin, ref, "OTHERSHIP", "SEO")
    assert (no_market["market_id"], no_market["market_name"]) == (None, None)

    detail = client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()
    assert detail["market_name"] == "London"

    moved = client.patch(f"/api/initiatives/{created['id']}", json={"market_id": markets["KOREA"]}, headers=admin["headers"])
    assert moved.status_code == 200, moved.text
    assert moved.json()["market_name"] == "Korea"
    audit = run_sql(
        "SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'UPDATE'",
        created["id"],
    )
    assert json.loads(audit[0]["old_value"]) == {"market_id": markets["LONDON"]}
    assert json.loads(audit[0]["new_value"]) == {"market_id": markets["KOREA"]}

    korea = client.get("/api/initiatives", params={"market_id": markets["KOREA"], "limit": 200}, headers=admin["headers"]).json()
    assert created["id"] in {i["id"] for i in korea["items"]}
    assert no_market["id"] not in {i["id"] for i in korea["items"]}
    # No market counts as Unassigned
    unassigned = client.get("/api/initiatives", params={"market_id": markets["UNASSIGNED"], "search": no_market["name"]}, headers=admin["headers"]).json()
    assert [i["id"] for i in unassigned["items"]] == [no_market["id"]]

    cleared = client.patch(f"/api/initiatives/{created['id']}", json={"market_id": None}, headers=admin["headers"])
    assert cleared.json()["market_id"] is None

    unknown = client.patch(f"/api/initiatives/{created['id']}", json={"market_id": 999999}, headers=admin["headers"])
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "Unknown market."


# --- Budgets ---


def test_create_budget(client, admin, ref, markets):
    response = _budget(client, admin, "2097-09-01", markets["LONDON"], amount=5000, currency="usd", brand_id=ref["brands"]["OTHERSHIP"])

    assert response.status_code == 201, response.text
    budget = response.json()
    assert budget["month"] == "2097-09-01"
    assert (budget["brand_name"], budget["market_name"], budget["channel_name"]) == ("Othership", "London", None)
    assert (budget["amount"], budget["currency"], budget["status"]) == (5000, "USD", "APPROVED")
    assert budget["progress"] == "NOT_STARTED"
    assert (budget["moved_in"], budget["moved_out"], budget["available"], budget["spent"], budget["remaining"]) == (0, 0, 5000, 0, 5000)
    assert budget["percent_used"] == 0
    assert budget["over_budget"] is False
    assert budget["other_currency"] == [] and budget["comment_count"] == 0 and budget["latest_comment"] is None
    assert budget["last_update_by_name"] == "Pytest User"

    assert _audit_titles("MonthlyBudget", budget["id"]) == ["Budget · Sep 2097 · Othership · London"]


def test_duplicate_budget(client, admin, ref, markets):
    _make_budget(client, admin, "2097-09-01", markets["LONDON"], brand_id=ref["brands"]["OTHERSHIP"])
    again = _budget(client, admin, "2097-09-01", markets["LONDON"], brand_id=ref["brands"]["OTHERSHIP"], amount=10)
    assert again.status_code == 422
    assert again.json()["detail"] == "There's already a budget for Othership · London in September 2097."

    _make_budget(client, admin, "2097-09-01", markets["LONDON"])
    # The TBD/All brand from the brand list means no brand
    tbd = _budget(client, admin, "2097-09-01", markets["LONDON"], brand_id=ref["brands"]["TBD_ALL"])
    assert tbd.status_code == 422
    assert tbd.json()["detail"] == "There's already a budget for TBD/All · London in September 2097."

    # A channel makes it a different budget
    channel = _budget(client, admin, "2097-09-01", markets["LONDON"], channel_id=ref["channels"]["SEO"])
    assert channel.status_code == 201
    assert channel.json()["channel_name"] == "SEO"


def test_budget_validation(client, admin, markets):
    assert _budget(client, admin, "2097-09-15", markets["LONDON"]).status_code == 422
    assert _budget(client, admin, "2097-09-01", markets["LONDON"], amount=-1).status_code == 422
    assert _budget(client, admin, "2097-09-01", 999999).json()["detail"] == "Unknown market."
    assert client.get("/api/budgets", params={"month": "2097-09-15"}, headers=admin["headers"]).status_code == 422


def test_patch_and_delete_budget(client, admin, ref, markets):
    budget = _make_budget(client, admin, "2097-10-01", markets["LONDON"], brand_id=ref["brands"]["OTHERSHIP"])

    response = client.patch(
        f"/api/budgets/{budget['id']}",
        json={"amount": 7500, "status": "NOT_COMMITTED", "channel_id": ref["channels"]["SEO"]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert (updated["amount"], updated["status"], updated["progress"], updated["channel_name"]) == (7500, "NOT_COMMITTED", "NOT_COMMITTED", "SEO")

    audit = run_sql(
        "SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'MonthlyBudget' AND entity_id = $1 AND action = 'UPDATE'",
        budget["id"],
    )
    old, new = json.loads(audit[0]["old_value"]), json.loads(audit[0]["new_value"])
    assert (old["amount"], old["status"], old["channel_id"]) == (1000, "APPROVED", None)
    assert (new["amount"], new["status"], new["channel_id"]) == (7500, "NOT_COMMITTED", ref["channels"]["SEO"])

    assert client.patch(f"/api/budgets/{budget['id']}", json={"market_id": None}, headers=admin["headers"]).status_code == 422

    assert client.delete(f"/api/budgets/{budget['id']}", headers=admin["headers"]).status_code == 204
    assert client.get(f"/api/budgets/{budget['id']}", headers=admin["headers"]).status_code == 404
    # The audit entry still reads sensibly once the budget is gone
    assert _audit_titles("MonthlyBudget", budget["id"])[-1] == "Budget · Oct 2097 · Othership · London · SEO"


# --- Which budget a cost counts against ---


def test_matching_rule(client, admin, ref, markets, category_id):
    month, day = "2097-03-01", date(2097, 3, 14)
    london, othership = markets["LONDON"], ref["brands"]["OTHERSHIP"]
    search, seo = ref["channels"]["PAID_SEARCH"], ref["channels"]["SEO"]
    exact = _make_budget(client, admin, month, london, brand_id=othership, channel_id=search)
    brand = _make_budget(client, admin, month, london, brand_id=othership)
    tbd = _make_budget(client, admin, month, london)
    # A cancelled budget never matches, however specific
    cancelled = _make_budget(client, admin, month, london, brand_id=othership, channel_id=seo, status="CANCELLED")

    othership_search = _initiative(client, admin, ref, "OTHERSHIP", "PAID_SEARCH", london)
    othership_seo = _initiative(client, admin, ref, "OTHERSHIP", "SEO", london)
    deskflex_search = _initiative(client, admin, ref, "DESKFLEX", "PAID_SEARCH", london)
    deskflex_no_market = _initiative(client, admin, ref, "DESKFLEX", "SEO")

    _line(othership_search, category_id, 100, day, status="PAID")
    _line(othership_search, category_id, 40, day, status="INVOICED")
    _line(othership_search, category_id, 50, day, status="ESTIMATED")
    _line(othership_search, category_id, 70, day, status="PAID", currency="GBP")  # listed, not counted
    _line(othership_search, category_id, 999, date(2097, 4, 1), status="PAID")  # next month
    _line(othership_seo, category_id, 300, day, status="COMMITTED")
    _line(deskflex_search, category_id, 25, day, status="INVOICED")
    _line(deskflex_no_market, category_id, 60, day, status="PAID")

    view = _month(client, admin, month)
    budgets = _by_id(view)

    a = budgets[exact["id"]]
    assert (a["spent"], a["expected"]) == (140, 190)
    assert a["other_currency"] == [{"currency": "GBP", "spent": 70, "expected": 70}]
    assert a["progress"] == "IN_PROGRESS"
    b = budgets[brand["id"]]
    assert (b["spent"], b["expected"], b["progress"]) == (0, 300, "NOT_STARTED")
    c = budgets[tbd["id"]]
    assert (c["spent"], c["expected"]) == (25, 25)

    # No market counts as Unassigned, which has no budget here
    assert view["unbudgeted"] == [{
        "brand_id": ref["brands"]["DESKFLEX"], "brand_name": "DeskFlex",
        "market_id": markets["UNASSIGNED"], "market_name": "Unassigned",
        "channel_id": seo, "channel_name": "SEO",
        "currency": "USD", "spent": 60, "expected": 60, "initiative_count": 1,
    }]

    # Ordered by market, then brand (TBD/All last), then channel (all channels last); cancelled is still listed
    assert [b["id"] for b in view["budgets"]] == [exact["id"], cancelled["id"], brand["id"], tbd["id"]]
    # Cancelled budgets aren't in the totals
    assert view["totals"] == [{"currency": "USD", "amount": 3000, "available": 3000, "spent": 165, "expected": 515, "remaining": 2835}]

    detail = client.get(f"/api/budgets/{exact['id']}", headers=admin["headers"]).json()
    assert [(s["initiative_id"], s["currency"], s["spent"], s["expected"], s["line_count"]) for s in detail["spend"]] == [
        (othership_search["id"], "USD", 140, 190, 3),
        (othership_search["id"], "GBP", 70, 70, 1),
    ]
    assert detail["spend"][0]["channel_name"] == "Paid Search"


def test_travel_is_outside_the_budget(client, admin, ref, markets, category_id):
    """Travel, per diems and ground transport count toward event cost, never against a marketing budget, whether
    they came from a travel feed or were entered here."""
    month, day = "2097-05-01", date(2097, 5, 12)
    london = markets["LONDON"]
    budget = _make_budget(client, admin, month, london, amount=1000)
    event = _initiative(client, admin, ref, "OTHERSHIP", "EVENTS", london)
    no_budget = _initiative(client, admin, ref, "DESKFLEX", "SEO", markets["KOREA"])
    hotels, per_diems = (
        run_sql("SELECT id FROM spend_categories WHERE code = $1", code)[0]["id"] for code in ("HOTELS", "PER_DIEMS_MEALS")
    )

    _line(event, category_id, 400, day, status="PAID")  # a booth cost: counts
    _line(event, hotels, 300, day, status="PAID")
    _line(event, per_diems, 50, day, status="INVOICED")
    run_sql(
        "INSERT INTO spend_lines (initiative_id, category_id, amount, currency, incurred_on, status, source, external_ref) "
        "VALUES ($1, $2, 120, 'USD', $3, 'COMMITTED', 'TRAVEL_FEED', $4)",
        uuid.UUID(event["id"]), category_id, day, f"pytest-{uuid.uuid4().hex[:8]}",
    )
    _line(no_budget, hotels, 80, day, status="PAID")  # travel with no budget is not "spend without a budget"

    view = _month(client, admin, month)

    b = _by_id(view)[budget["id"]]
    assert (b["spent"], b["expected"], b["remaining"]) == (400, 400, 600)
    assert view["unbudgeted"] == []
    assert view["outside_budget"] == [{"currency": "USD", "spent": 430, "expected": 550, "initiative_count": 2}]
    detail = client.get(f"/api/budgets/{budget['id']}", headers=admin["headers"]).json()
    assert [(s["initiative_id"], s["expected"], s["line_count"]) for s in detail["spend"]] == [(event["id"], 400, 1)]

    monday = day - timedelta(days=day.weekday())
    week = client.get("/api/budgets/week", params={"start": monday.isoformat()}, headers=admin["headers"]).json()
    flags = sorted((line["amount"], line["budget_id"], line["outside_budget"]) for line in week["lines"])
    assert flags == [(50, None, True), (80, None, True), (120, None, True), (300, None, True), (400, budget["id"], False)]


def test_derived_figures_and_progress(client, admin, ref, markets, category_id):
    othership = ref["brands"]["OTHERSHIP"]
    past = _make_budget(client, admin, "2001-05-01", markets["LONDON"], amount=1000, brand_id=othership)
    later = _make_budget(client, admin, "2001-06-01", markets["LONDON"], amount=500, brand_id=othership)
    idle = _make_budget(client, admin, "2001-05-01", markets["KOREA"], amount=0)
    initiative = _initiative(client, admin, ref, "OTHERSHIP", "SEO", markets["LONDON"])
    _line(initiative, category_id, 900, date(2001, 5, 20), status="INVOICED")

    move = client.post(
        "/api/budget-transfers",
        json={"from_budget_id": past["id"], "to_budget_id": later["id"], "amount": 200, "note": "  Carryover  "},
        headers=admin["headers"],
    )
    assert move.status_code == 201, move.text
    assert (move.json()["amount"], move.json()["note"], move.json()["created_by_name"]) == (200, "Carryover", "Pytest User")

    may = _by_id(_month(client, admin, "2001-05-01"))
    row = may[past["id"]]
    assert (row["moved_in"], row["moved_out"], row["available"], row["spent"], row["remaining"]) == (0, 200, 800, 900, -100)
    assert row["percent_used"] == 112.5
    assert row["over_budget"] is True
    assert row["progress"] == "COMPLETE"  # the month has ended
    assert may[idle["id"]]["percent_used"] is None  # nothing available
    assert may[idle["id"]]["progress"] == "COMPLETE"

    june = _month(client, admin, "2001-06-01")
    assert june["previous_month_has_budgets"] is True
    row = june["budgets"][0]
    assert (row["moved_in"], row["available"], row["over_budget"]) == (200, 700, False)

    detail = client.get(f"/api/budgets/{later['id']}", headers=admin["headers"]).json()
    assert [(t["direction"], t["other_budget_id"], t["other_budget_label"], t["amount"]) for t in detail["transfers"]] == [
        ("IN", past["id"], "May 2001 · Othership · London", 200),
    ]
    assert _audit_titles("BudgetTransfer", move.json()["id"]) == ["Move money to Jun 2001 · Othership · London"]


# --- Copy from last month ---


def test_copy_month(client, admin, ref, markets):
    othership = ref["brands"]["OTHERSHIP"]
    _make_budget(client, admin, "2098-01-01", markets["LONDON"], amount=100, brand_id=othership)
    _make_budget(client, admin, "2098-01-01", markets["GERMANY"], amount=200, currency="EUR")
    _make_budget(client, admin, "2098-01-01", markets["KOREA"], amount=300, status="CANCELLED")
    existing = _make_budget(client, admin, "2098-02-01", markets["GERMANY"], amount=999, currency="EUR")

    assert _month(client, admin, "2098-02-01")["previous_month_has_budgets"] is True
    assert _month(client, admin, "2098-01-01")["previous_month_has_budgets"] is False

    response = client.post("/api/budgets/copy", json={"from_month": "2098-01-01", "to_month": "2098-02-01"}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"created": 1, "skipped": 1}

    february = _month(client, admin, "2098-02-01")["budgets"]
    assert [(b["market_name"], b["brand_name"], b["amount"], b["currency"], b["status"], b["progress"]) for b in february] == [
        ("London", "Othership", 100, "USD", "NOT_COMMITTED", "NOT_COMMITTED"),
        ("Germany", None, 999, "EUR", "APPROVED", "NOT_STARTED"),
    ]
    assert existing["id"] in {b["id"] for b in february}

    copied = next(b for b in february if b["market_name"] == "London")
    assert _audit_titles("MonthlyBudget", copied["id"]) == ["Budget · Feb 2098 · Othership · London"]

    again = client.post("/api/budgets/copy", json={"from_month": "2098-01-01", "to_month": "2098-02-01"}, headers=admin["headers"])
    assert again.json() == {"created": 0, "skipped": 2}
    same = client.post("/api/budgets/copy", json={"from_month": "2098-01-01", "to_month": "2098-01-01"}, headers=admin["headers"])
    assert same.status_code == 422


# --- Money moves ---


def test_transfer_rules(client, admin, markets):
    usd = _make_budget(client, admin, "2097-05-01", markets["LONDON"], amount=100)
    usd_other = _make_budget(client, admin, "2097-05-01", markets["KOREA"], amount=100)
    gbp = _make_budget(client, admin, "2097-05-01", markets["GERMANY"], amount=100, currency="GBP")

    def move(source, target, amount=10, note="Borrowing"):
        return client.post(
            "/api/budget-transfers",
            json={"from_budget_id": source["id"], "to_budget_id": target["id"], "amount": amount, "note": note},
            headers=admin["headers"],
        )

    assert move(usd, usd).json()["detail"] == "Choose two different budgets."
    assert move(usd, gbp).json()["detail"] == "Money can only move between budgets in the same currency."
    assert move(usd, usd_other, amount=0).status_code == 422
    assert move(usd, usd_other, note="   ").status_code == 422
    assert move(usd, {"id": str(uuid.uuid4())}).json()["detail"] == "That budget no longer exists."

    transfer = move(usd, usd_other)
    assert transfer.status_code == 201

    # The budget can't go, or change currency, while money moves use it
    refused = client.delete(f"/api/budgets/{usd['id']}", headers=admin["headers"])
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Remove the money moves on this budget first."
    currency = client.patch(f"/api/budgets/{usd_other['id']}", json={"currency": "EUR"}, headers=admin["headers"])
    assert currency.status_code == 422
    # Other edits are fine
    assert client.patch(f"/api/budgets/{usd_other['id']}", json={"amount": 150}, headers=admin["headers"]).status_code == 200

    assert client.delete(f"/api/budget-transfers/{transfer.json()['id']}", headers=admin["headers"]).status_code == 204
    assert client.delete(f"/api/budget-transfers/{transfer.json()['id']}", headers=admin["headers"]).status_code == 404
    assert client.delete(f"/api/budgets/{usd['id']}", headers=admin["headers"]).status_code == 204


# --- Comments and permissions ---


def test_comments(client, admin, initiative_viewer, markets):
    budget = _make_budget(client, admin, "2097-07-01", markets["LONDON"])
    viewer = initiative_viewer

    def comment(user, **body):
        return client.post("/api/budget-comments", json=body, headers=user["headers"])

    # Anyone who can see budgets can comment; the month comes from the budget
    mine = comment(viewer, budget_id=budget["id"], month="2001-01-01", body="  Waiting on the Q3 numbers.  ")
    assert mine.status_code == 201, mine.text
    assert (mine.json()["month"], mine.json()["body"], mine.json()["author_name"]) == ("2097-07-01", "Waiting on the Q3 numbers.", "Pytest User")
    theirs = comment(admin, budget_id=budget["id"], body="Approved by Finance.").json()
    note = comment(admin, month="2097-07-01", body="July budgets agreed on the call.")
    assert note.status_code == 201
    assert note.json()["budget_id"] is None

    assert comment(admin, body="No target").status_code == 422
    assert comment(admin, month="2097-07-01", body="   ").status_code == 422
    assert comment(admin, month="2097-07-01", body="x" * 2001).status_code == 422
    assert comment(admin, budget_id=str(uuid.uuid4()), body="Hello").status_code == 404

    view = _month(client, viewer, "2097-07-01")
    assert [n["body"] for n in view["notes"]] == ["July budgets agreed on the call."]
    row = view["budgets"][0]
    assert row["comment_count"] == 2
    assert row["latest_comment"]["body"] == "Approved by Finance."
    detail = client.get(f"/api/budgets/{budget['id']}", headers=viewer["headers"]).json()
    assert [c["body"] for c in detail["comments"]] == ["Approved by Finance.", "Waiting on the Q3 numbers."]

    assert _audit_titles("BudgetComment", theirs["id"]) == ["Comment · Jul 2097 · TBD/All · London"]
    assert _audit_titles("BudgetComment", note.json()["id"]) == ["Comment · Jul 2097"]

    # Only the author (or a super admin) can delete a comment
    refused = client.delete(f"/api/budget-comments/{theirs['id']}", headers=viewer["headers"])
    assert refused.status_code == 403
    assert client.delete(f"/api/budget-comments/{mine.json()['id']}", headers=viewer["headers"]).status_code == 204
    assert client.delete(f"/api/budget-comments/{theirs['id']}", headers=admin["headers"]).status_code == 204

    # Comments go with the budget
    comment(admin, budget_id=budget["id"], body="One more.")
    assert client.delete(f"/api/budgets/{budget['id']}", headers=admin["headers"]).status_code == 204
    assert run_sql("SELECT count(*) AS n FROM budget_comments WHERE budget_id = $1", uuid.UUID(budget["id"]))[0]["n"] == 0


def test_viewer_cannot_change_budgets(client, admin, initiative_viewer, markets):
    budget = _make_budget(client, admin, "2097-08-01", markets["LONDON"])
    other = _make_budget(client, admin, "2097-08-01", markets["KOREA"])
    headers = initiative_viewer["headers"]

    assert client.get("/api/budgets", params={"month": "2097-08-01"}, headers=headers).status_code == 200
    assert client.get(f"/api/budgets/{budget['id']}", headers=headers).status_code == 200
    assert _budget(client, initiative_viewer, "2097-08-01", markets["GERMANY"]).status_code == 403
    assert client.patch(f"/api/budgets/{budget['id']}", json={"amount": 1}, headers=headers).status_code == 403
    assert client.delete(f"/api/budgets/{budget['id']}", headers=headers).status_code == 403
    assert client.post("/api/budgets/copy", json={"from_month": "2097-08-01", "to_month": "2097-09-01"}, headers=headers).status_code == 403
    move = {"from_budget_id": budget["id"], "to_budget_id": other["id"], "amount": 1, "note": "Borrowing"}
    assert client.post("/api/budget-transfers", json=move, headers=headers).status_code == 403


def test_budgets_need_initiative_access(client, viewer):
    # A regular user with no Marketing permissions
    assert client.get("/api/budgets", params={"month": "2097-08-01"}, headers=viewer["headers"]).status_code == 403
    assert client.post("/api/budget-comments", json={"month": "2097-08-01", "body": "Hi"}, headers=viewer["headers"]).status_code == 403
    assert client.get("/api/budgets", params={"month": "2097-08-01"}).status_code == 401


# --- Week view ---


def test_week_view(client, admin, ref, markets, category_id):
    """A week that runs from October into November: each cost on its day with the budget it counts against, and
    October's budget so far, which includes spend from before the week."""
    monday = next(date(2090, 10, 25) + timedelta(days=n) for n in range(7) if (date(2090, 10, 25) + timedelta(days=n)).weekday() == 0)
    assert monday.month == 10 and (monday + timedelta(days=6)).month == 11
    budget = _make_budget(client, admin, "2090-10-01", markets["NORTH_AMERICA"], amount=1000,
                          brand_id=ref["brands"]["DESKFLEX"], channel_id=ref["channels"]["PAID_SEARCH"])
    ads = _initiative(client, admin, ref, "DESKFLEX", "PAID_SEARCH", market_id=markets["NORTH_AMERICA"])
    _line(ads, category_id, 300, date(2090, 10, 2), status="INVOICED")  # earlier in October
    _line(ads, category_id, 200, date(2090, 10, 31), status="PAID")
    _line(ads, category_id, 50, date(2090, 11, 1))  # November has no budget

    response = client.get("/api/budgets/week", params={"start": monday.isoformat()}, headers=admin["headers"])

    assert response.status_code == 200, response.text
    week = response.json()
    assert (week["start"], week["end"]) == (monday.isoformat(), (monday + timedelta(days=6)).isoformat())
    lines = [(line["incurred_on"], line["amount"], line["budget_id"]) for line in week["lines"]]
    assert lines == [("2090-10-31", 200, budget["id"]), ("2090-11-01", 50, None)]
    assert week["lines"][0]["budget_label"] == "Oct 2090 · DeskFlex · North America · Paid Search"
    assert week["lines"][0]["initiative_name"] == ads["name"]
    assert week["totals"] == [{"currency": "USD", "spent": 200, "expected": 250}]
    assert week["months"] == [{
        "month": "2090-10-01", "currency": "USD", "available": 1000, "spent_to_date": 500,
        "through": "2090-10-31", "days_in_month": 31,
    }]


def test_week_starts_on_a_monday(client, admin):
    response = client.get("/api/budgets/week", params={"start": "2090-10-04"}, headers=admin["headers"])
    assert response.status_code == 422
    assert "Monday" in response.json()["detail"]
