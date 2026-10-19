"""Results, success settings, decisions and the performance view:
/api/initiatives/{id}/outcomes, /evaluation, /decisions and /performance."""
import json
import uuid

import pytest

from tests.conftest import run_sql


@pytest.fixture(scope="module")
def codes():
    """Ids by code for the lists these tests use."""
    return {
        "categories": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM spend_categories")},
        "demo_types": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM demo_types")},
        "options": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM scale_or_cut_options")},
        "metrics": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM outcome_metrics")},
    }


def _event(client, admin, ref, **overrides):
    """An in-person event that ended a year ago, so its maturity window has passed."""
    body = {
        "name": f"Pytest Results Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": "2025-09-04",
        "end_date": "2025-09-06",
        "budget_amount": 20000,
        "currency": "USD",
        "event": {"city": "Las Vegas", "country_code": "US"},
        **overrides,
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def event(client, admin, ref):
    return _event(client, admin, ref)


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user, given only the "View Initiatives" permission group."""
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


def _cost(client, admin, initiative_id, codes, amount, currency=None):
    body = {"category_id": codes["categories"]["BOOTH_SPACE"], "amount": amount, "incurred_on": "2025-08-01"}
    if currency:
        body["currency"] = currency
    response = client.post(f"/api/initiatives/{initiative_id}/spend-lines", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text


def _save(client, user, initiative_id, period, values):
    return client.put(f"/api/initiatives/{initiative_id}/outcomes/{period}", json={"values": values}, headers=user["headers"])


def _performance(client, user, initiative_id):
    response = client.get(f"/api/initiatives/{initiative_id}/performance", headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()


# --- Results ---


def test_save_and_read_results(client, admin, event):
    response = _save(client, admin, event["id"], "2025-09-01", [
        {"metric_code": "leads", "value": 200},
        {"metric_code": "REVENUE", "value": 20000},
    ])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["periods"] == ["2025-09-01"]
    values = {v["metric_code"]: v for v in body["values"]}
    assert values["LEADS"]["value"] == 200
    assert values["LEADS"]["currency"] is None
    # Money results take the initiative's currency
    assert values["REVENUE"]["currency"] == "USD"
    assert values["LEADS"]["source"] == "MANUAL"
    assert values["LEADS"]["recorded_by_name"] == "Pytest User"

    audit = run_sql("SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'Outcomes' AND entity_id = $1", event["id"])
    assert len(audit) == 1
    assert json.loads(audit[0]["new_value"])["LEADS"] == 200


def test_update_changes_only_what_was_sent_and_null_removes(client, admin, event):
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 200}, {"metric_code": "VISITORS", "value": 600}])

    body = _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 210}, {"metric_code": "VISITORS", "value": None}]).json()

    assert {v["metric_code"]: v["value"] for v in body["values"]} == {"LEADS": 210}
    # Sending the same value again changes nothing and isn't audited
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 210}])
    assert len(run_sql("SELECT 1 FROM audit_logs WHERE entity_type = 'Outcomes' AND entity_id = $1", event["id"])) == 2


@pytest.mark.parametrize(
    "period, values, message",
    [
        ("2025-09-15", [{"metric_code": "LEADS", "value": 1}], "Results are recorded per month"),
        ("2025-09-01", [{"metric_code": "BANANAS", "value": 1}], "Unknown result: BANANAS."),
        ("2025-09-01", [{"metric_code": "LEADS", "value": 1}, {"metric_code": "LEADS", "value": 2}], "only be sent once"),
    ],
)
def test_save_results_rejects_bad_input(client, admin, event, period, values, message):
    response = _save(client, admin, event["id"], period, values)
    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_negative_result_is_rejected(client, admin, event):
    assert _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": -1}]).status_code == 422


def test_synced_values_are_untouched_and_win(client, admin, event, codes):
    """A HubSpot number and a typed-in one for the same month: the synced one counts, and saving never removes it."""
    run_sql(
        "INSERT INTO outcomes (initiative_id, period_start, metric_id, value, source) VALUES ($1, '2025-09-01', $2, 250, 'HUBSPOT')",
        uuid.UUID(event["id"]), codes["metrics"]["LEADS"],
    )
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 200}])
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": None}])
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 180}])

    sources = {v["source"]: v["value"] for v in client.get(f"/api/initiatives/{event['id']}/outcomes", headers=admin["headers"]).json()["values"]}
    assert sources == {"HUBSPOT": 250, "MANUAL": 180}
    assert _performance(client, admin, event["id"])["totals"]["LEADS"] == 250


def test_demos_come_from_the_demo_log(client, admin, event, codes):
    for _ in range(3):
        client.post(
            f"/api/initiatives/{event['id']}/demos",
            json={"demo_type_id": codes["demo_types"]["BOOTH"], "demo_at": "2025-09-05T10:00:00Z"},
            headers=admin["headers"],
        )

    rejected = _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "DEMOS", "value": 20}])
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "Demos are counted from the demo log on this event."

    body = client.get(f"/api/initiatives/{event['id']}/outcomes", headers=admin["headers"]).json()
    assert body["demos_from_log"] is True
    assert [(v["period_start"], v["value"], v["source"]) for v in body["values"]] == [("2025-09-01", 3, "DEMO_LOG")]
    assert _performance(client, admin, event["id"])["totals"]["DEMOS"] == 3


def test_demos_can_be_typed_in_without_a_log(client, admin, event):
    assert _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "DEMOS", "value": 20}]).status_code == 200
    assert _performance(client, admin, event["id"])["totals"]["DEMOS"] == 20


def test_typed_in_demos_stop_counting_once_there_is_a_log(client, admin, event, codes):
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "DEMOS", "value": 50}])
    # Only a booking for later so far: nothing has happened, and the typed-in 50 no longer counts
    client.post(
        f"/api/initiatives/{event['id']}/demos",
        json={"demo_type_id": codes["demo_types"]["SCHEDULED"], "demo_at": "2099-01-05T10:00:00Z"},
        headers=admin["headers"],
    )

    p = _performance(client, admin, event["id"])
    assert "DEMOS" not in p["totals"]
    assert p["demos_from_log"] is True


def test_zero_revenue_counts_as_revenue(client, admin, event, codes):
    """Revenue recorded as 0 once the window is over: it lost money, not "no revenue yet"."""
    _cost(client, admin, event["id"], codes, 5000)
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 100}, {"metric_code": "REVENUE", "value": 0}])

    p = _performance(client, admin, event["id"])
    assert p["roi_percent"] == -100.0
    assert p["money_outcome"] == "LOST_MONEY"
    assert p["rating"]["code"] == "LOW"


# --- Performance ---


def test_ceo_example(client, admin, event, codes):
    """$20,000 trade show: 200 leads, 20 demos, 2 customers, $20,000 revenue → $100 / $1,000 / $10,000, broke even, Low."""
    _cost(client, admin, event["id"], codes, 20000)
    for i in range(20):
        client.post(
            f"/api/initiatives/{event['id']}/demos",
            json={"demo_type_id": codes["demo_types"]["BOOTH"], "demo_at": f"2025-09-0{4 + i % 3}T1{i % 10}:00:00Z"},
            headers=admin["headers"],
        )
    _save(client, admin, event["id"], "2025-09-01", [
        {"metric_code": "LEADS", "value": 200},
        {"metric_code": "CUSTOMERS", "value": 2},
        {"metric_code": "REVENUE", "value": 20000},
    ])
    p = _performance(client, admin, event["id"])

    assert p["cost"] == 20000
    ladder = {s["code"]: s for s in p["ladder"]}
    assert [s["code"] for s in p["ladder"]] == ["LEADS", "DEMOS", "CUSTOMERS"]
    assert (ladder["LEADS"]["cost_per"], ladder["DEMOS"]["cost_per"], ladder["CUSTOMERS"]["cost_per"]) == (100, 1000, 10000)
    assert (ladder["DEMOS"]["from_previous"], ladder["CUSTOMERS"]["from_previous"]) == (10.0, 10.0)
    assert p["roi_percent"] == 0.0
    assert p["money_outcome"] == "BROKE_EVEN"
    # Breaking even isn't a success
    assert p["rating"]["code"] == "LOW"
    assert "broke even" in p["rating"]["reason"]
    assert p["suggested_decision"] == "PAUSE"
    assert p["demos_from_log"] is True


def test_high_rating_with_a_positive_return(client, admin, ref, codes):
    sponsorship = _event(client, admin, ref, sub_channel_id=ref["sub_channels"]["SPONSORSHIPS"], event={"country_code": "AE"})
    _cost(client, admin, sponsorship["id"], codes, 13500)
    _save(client, admin, sponsorship["id"], "2025-09-01", [
        {"metric_code": "LEADS", "value": 110},
        {"metric_code": "CUSTOMERS", "value": 3},
        {"metric_code": "REVENUE", "value": 36000},
    ])
    client.put(f"/api/initiatives/{sponsorship['id']}/evaluation", json={"maturity_days": 120}, headers=admin["headers"])

    p = _performance(client, admin, sponsorship["id"])

    assert p["roi_percent"] == 166.7
    assert p["money_outcome"] == "PROFIT"
    assert p["rating"] == {"code": "HIGH", "name": "High", "reason": "It made money: ROI 166.7%."}
    assert p["suggested_decision"] == "SCALE"
    assert not any("kpi" in key for key in p) and "early_read" not in p


def test_no_revenue_after_the_window_is_test(client, admin, event, codes):
    """Judged on the return only: without revenue there's no verdict, whatever the leads or demos."""
    _cost(client, admin, event["id"], codes, 5000)
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 500}])

    p = _performance(client, admin, event["id"])

    assert p["stage"] == "FINAL"
    assert p["rating"]["code"] == "TEST"
    assert p["rating"]["reason"].startswith("No revenue recorded yet")
    assert p["suggested_decision"] == "TEST_MORE"


def test_upcoming_event_has_no_verdict(client, admin, ref, codes):
    upcoming = _event(client, admin, ref, start_date="2030-11-04", end_date="2030-11-06")
    _cost(client, admin, upcoming["id"], codes, 5000)

    p = _performance(client, admin, upcoming["id"])

    assert p["stage"] == "UPCOMING"
    assert p["rating"]["code"] == "TEST"
    assert p["rating"]["reason"].startswith("Not started yet.")
    assert p["matures_on"] == "2031-05-05"
    assert p["money_outcome"] == "NO_REVENUE"
    assert p["has_results"] is False


def test_no_verdict_inside_the_maturity_window(client, admin, ref, codes):
    """Just after an event: the final verdict comes once deals have closed."""
    from datetime import date, timedelta

    start = date.today() - timedelta(days=12)
    recent = _event(
        client, admin, ref, start_date=start.isoformat(), end_date=(start + timedelta(days=2)).isoformat(),
        sub_channel_id=ref["sub_channels"]["SPONSORSHIPS"], event={"country_code": "AE"},
    )
    _cost(client, admin, recent["id"], codes, 10000)
    _save(client, admin, recent["id"], f"{start.isoformat()[:7]}-01", [{"metric_code": "LEADS", "value": 80}])
    client.put(f"/api/initiatives/{recent['id']}/evaluation", json={"maturity_days": 180}, headers=admin["headers"])

    p = _performance(client, admin, recent["id"])

    assert p["stage"] == "EARLY"
    assert p["rating"]["code"] == "TEST"
    assert "final verdict is due on" in p["rating"]["reason"]


def test_a_kpi_sent_by_an_older_client_is_ignored(client, admin, ref):
    """No KPI is set by hand any more: a primary_kpi_id sent anyway changes nothing."""
    response = client.post(
        "/api/initiatives",
        json={"name": "Pytest Old Client Expo", "brand_id": ref["brands"]["DESKFLEX"], "channel_id": ref["channels"]["EVENTS"],
              "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"], "primary_kpi_id": 1,
              "event": {"city": "Las Vegas", "country_code": "US"}},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    assert not any("kpi" in key for key in response.json())
    run_sql("DELETE FROM initiatives WHERE id = $1", uuid.UUID(response.json()["id"]))


def test_choices_by_channel_use_real_codes(client, admin):
    """Every channel and result named in the rules exists, so a typo can't quietly hide a choice."""
    body = client.get("/api/reference-data", headers=admin["headers"]).json()
    channels = {c["code"] for c in body["channels"]}
    results = {m["code"] for m in body["outcome_metrics"]}
    assert set(body["result_metrics_by_channel"]) <= channels
    assert {code for codes in body["result_metrics_by_channel"].values() for code in codes} <= results
    assert "IMPRESSIONS" not in body["result_metrics_by_channel"]["EVENTS"]


def test_results_must_fit_the_channel(client, admin, event):
    """No ad impressions or clicks on a trade show."""
    response = _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "IMPRESSIONS", "value": 5000}, {"metric_code": "CLICKS", "value": 50}])
    assert response.status_code == 422
    assert response.json()["detail"] == "Events doesn't record Impressions, Clicks."


def test_an_older_misfit_result_can_be_cleared(client, admin, event, codes):
    # Saved before the rule existed
    run_sql(
        "INSERT INTO outcomes (initiative_id, period_start, metric_id, value, source) VALUES ($1, '2025-09-01', $2, 5000, 'MANUAL')",
        uuid.UUID(event["id"]), codes["metrics"]["IMPRESSIONS"],
    )
    assert _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "IMPRESSIONS", "value": None}]).status_code == 200
    assert client.get(f"/api/initiatives/{event['id']}/outcomes", headers=admin["headers"]).json()["values"] == []


def test_ad_ratios_only_where_the_channel_uses_them(client, admin, event, codes):
    """Even with old ad numbers on an event, it shows no click-through or cost per click."""
    _cost(client, admin, event["id"], codes, 1000)
    run_sql(
        "INSERT INTO outcomes (initiative_id, period_start, metric_id, value, source) "
        "VALUES ($1, '2025-09-01', $2, 10000, 'MANUAL'), ($1, '2025-09-01', $3, 100, 'MANUAL')",
        uuid.UUID(event["id"]), codes["metrics"]["IMPRESSIONS"], codes["metrics"]["CLICKS"],
    )
    p = _performance(client, admin, event["id"])
    assert (p["ctr"], p["cpc"], p["cpm"]) == (None, None, None)


def test_moving_channel_needs_results_that_fit(client, admin, ref):
    """A search ad with impressions can't become content marketing, which doesn't record them."""
    created = client.post(
        "/api/initiatives",
        json={"name": f"Pytest Search Ad {uuid.uuid4().hex[:6]}", "brand_id": ref["brands"]["DESKFLEX"],
              "channel_id": ref["channels"]["PAID_SEARCH"], "sub_channel_id": ref["sub_channels"]["GOOGLE_ADS"]},
        headers=admin["headers"],
    ).json()
    assert _save(client, admin, created["id"], "2025-09-01", [{"metric_code": "IMPRESSIONS", "value": 5000}, {"metric_code": "LEADS", "value": 5}]).status_code == 200

    move = {"channel_id": ref["channels"]["CONTENT_MARKETING"], "sub_channel_id": ref["sub_channels"]["BLOG"]}
    moved = client.patch(f"/api/initiatives/{created['id']}", json=move, headers=admin["headers"])
    assert moved.status_code == 422
    assert moved.json()["detail"] == "Content Marketing doesn't record Impressions. Remove those results before moving it."

    _save(client, admin, created["id"], "2025-09-01", [{"metric_code": "IMPRESSIONS", "value": None}])
    assert client.patch(f"/api/initiatives/{created['id']}", json=move, headers=admin["headers"]).status_code == 200


def test_other_currencies_are_reported_not_converted(client, admin, event, codes):
    _cost(client, admin, event["id"], codes, 1000)
    _cost(client, admin, event["id"], codes, 500, currency="GBP")
    _save(client, admin, event["id"], "2025-09-01", [
        {"metric_code": "LEADS", "value": 10},
        {"metric_code": "REVENUE", "value": 900, "currency": "GBP"},
    ])

    p = _performance(client, admin, event["id"])

    assert p["cost"] == 1000
    assert p["costs_excluded"] == [{"currency": "GBP", "amount": 500}]
    assert p["revenue"] is None
    assert p["revenue_excluded"] == [{"currency": "GBP", "amount": 900}]
    assert p["ladder"][0]["cost_per"] == 100


# --- Evaluation settings ---


def test_evaluation_defaults_by_sub_channel(client, admin, event):
    evaluation = client.get(f"/api/initiatives/{event['id']}/evaluation", headers=admin["headers"]).json()
    assert evaluation["maturity_days"] == 180
    assert evaluation["is_default"] is True
    assert set(evaluation) == {"maturity_days", "revenue_basis", "is_default", "updated_at", "updated_by_name"}


def test_save_evaluation(client, admin, event):
    response = client.put(f"/api/initiatives/{event['id']}/evaluation", json={"maturity_days": 90}, headers=admin["headers"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["maturity_days"], body["is_default"]) == (90, False)
    assert body["updated_by_name"] == "Pytest User"
    audit = run_sql("SELECT new_value FROM audit_logs WHERE entity_type = 'Evaluation' AND entity_id = $1", event["id"])
    assert json.loads(audit[0]["new_value"]) == {"maturity_days": 90}


def test_evaluation_rejects_out_of_range(client, admin, event):
    for body in ({"maturity_days": -1}, {"maturity_days": 731}):
        assert client.put(f"/api/initiatives/{event['id']}/evaluation", json=body, headers=admin["headers"]).status_code == 422


# --- Decisions ---


def test_record_decisions(client, admin, event, codes):
    first = client.post(
        f"/api/initiatives/{event['id']}/decisions",
        json={"scale_or_cut_id": codes["options"]["TEST_MORE"], "note": "  Wait for Q1 deals  "},
        headers=admin["headers"],
    )
    assert first.status_code == 201, first.text
    # No revenue yet, so the system's suggestion at the time was Test
    assert first.json()["suggested_rating"] == "TEST"
    assert first.json()["note"] == "Wait for Q1 deals"

    client.post(f"/api/initiatives/{event['id']}/decisions", json={"scale_or_cut_id": codes["options"]["SCALE"]}, headers=admin["headers"])

    decisions = client.get(f"/api/initiatives/{event['id']}/decisions", headers=admin["headers"]).json()
    assert [d["code"] for d in decisions] == ["SCALE", "TEST_MORE"]
    assert _performance(client, admin, event["id"])["latest_decision"]["code"] == "SCALE"
    assert len(run_sql("SELECT 1 FROM audit_logs WHERE entity_type = 'Decision' AND action = 'CREATE' AND new_value->>'initiative_id' = $1", event["id"])) == 2


def test_unknown_decision(client, admin, event):
    response = client.post(f"/api/initiatives/{event['id']}/decisions", json={"scale_or_cut_id": 999999}, headers=admin["headers"])
    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown decision."


# --- Access ---


def test_viewer_can_read_but_not_change(client, event, codes, initiative_viewer):
    for path in ("performance", "outcomes", "evaluation", "decisions"):
        assert client.get(f"/api/initiatives/{event['id']}/{path}", headers=initiative_viewer["headers"]).status_code == 200
    assert _save(client, initiative_viewer, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 1}]).status_code == 403
    assert client.put(f"/api/initiatives/{event['id']}/evaluation", json={"maturity_days": 90}, headers=initiative_viewer["headers"]).status_code == 403
    assert client.post(
        f"/api/initiatives/{event['id']}/decisions", json={"scale_or_cut_id": codes["options"]["SCALE"]}, headers=initiative_viewer["headers"]
    ).status_code == 403


def test_user_without_marketing_access_is_refused(client, event, viewer):
    assert client.get(f"/api/initiatives/{event['id']}/performance", headers=viewer["headers"]).status_code == 403


def test_missing_initiative(client, admin):
    missing = uuid.uuid4()
    for path in ("performance", "outcomes", "evaluation", "decisions"):
        assert client.get(f"/api/initiatives/{missing}/{path}", headers=admin["headers"]).status_code == 404
    assert _save(client, admin, missing, "2025-09-01", [{"metric_code": "LEADS", "value": 1}]).status_code == 404


def test_deleting_the_initiative_removes_everything(client, admin, event, codes):
    _save(client, admin, event["id"], "2025-09-01", [{"metric_code": "LEADS", "value": 1}])
    client.put(f"/api/initiatives/{event['id']}/evaluation", json={"maturity_days": 90}, headers=admin["headers"])
    client.post(f"/api/initiatives/{event['id']}/decisions", json={"scale_or_cut_id": codes["options"]["SCALE"]}, headers=admin["headers"])

    run_sql("DELETE FROM initiatives WHERE id = $1", uuid.UUID(event["id"]))

    for table in ("outcomes", "initiative_evaluations", "initiative_decisions"):
        assert run_sql(f"SELECT count(*) AS n FROM {table} WHERE initiative_id = $1", uuid.UUID(event["id"]))[0]["n"] == 0


def test_reference_data_has_results_lists(client, admin):
    data = client.get("/api/reference-data", headers=admin["headers"]).json()
    metrics = {m["code"]: m["unit"] for m in data["outcome_metrics"]}
    assert metrics["REVENUE"] == "MONEY" and metrics["LEADS"] == "COUNT"
    assert "primary_kpis" not in data and "kpi_choices_by_channel" not in data
    in_person = next(s["id"] for s in data["sub_channels"] if s["code"] == "IN_PERSON_EVENTS")
    default = next(d for d in data["evaluation_defaults"] if d["sub_channel_id"] == in_person)
    assert default["maturity_days"] == 180
