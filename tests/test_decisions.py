"""Wave 9b: recommendations with reasons and impact, owners' decisions, the track record, the AI assistant.

The scenario is the control-tower company of ``tests/test_control.py`` (fixture ``tower``), judged on
15 November 2026: a late customer order line (stock is there to complete it), an overdue purchase
line, press PRESS3 down, an urgent ticket overdue, steel single source.
"""
import logging
import urllib.error
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.models import Role
from apps.control import assistant, decisions, services
from apps.control.models import AssistantAnswer, Decision
from apps.organization.models import StorageLocation
from apps.sales import services as sales

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/control"
ON = date(2026, 11, 15)
NOW = datetime(2026, 11, 15, 8, 0, tzinfo=dt_timezone.utc)


def problem(tower, kind, **extra):
    """The first current problem of one kind (the part of its key before the first colon)."""
    return next(p for p in services.problems(tower.company, on=ON, now=NOW, **extra) if p["key"].startswith(kind))


def late_order_key(tower):
    return f"late-order:{tower.late_so.order_number}/10"


# --- recommendations ---------------------------------------------------------------------------------------

def test_every_problem_has_a_recommendation_with_reason_and_impact(tower):
    found = services.problems(tower.company, on=ON, now=NOW)
    assert found and all(p["recommendation"]["action"] and p["recommendation"]["reason"] for p in found)

    late = problem(tower, "late-order")
    advice = late["recommendation"]
    assert advice["action"].startswith("Deliver now: ") and "TG-1001 in stock at plant 1000" in advice["action"]
    assert advice["impact"]["kpi"] == "otif_customers" and advice["impact"]["value"] > 0
    assert late["owner_role"] == Role.SALES_REP

    overdue = problem(tower, "late-receipt")["recommendation"]
    assert overdue["impact"] == {"kpi": "otif_suppliers", "value": D("9000.00"),
                                 "text": "R9000.00 of purchases received"}  # 50 x R180
    assert "2 working days" in overdue["action"]

    cell = problem(tower, "cell")
    assert cell["key"].startswith("cell:1000/PRESS3:DOWN:2026") and "Send maintenance to PRESS3" in \
        cell["recommendation"]["action"]
    risk = problem(tower, "supplier-risk")["recommendation"]
    assert risk["impact"]["kpi"] == "revenue_at_risk" and risk["impact"]["value"] > 0
    assert "Qualify a second source" in risk["action"]


def test_a_late_order_without_stock_is_re_promised(tower, monkeypatch):
    """Not enough stock: the recommendation is to agree a new date, not to deliver."""
    from apps.inventory import services as inventory

    monkeypatch.setattr(inventory, "plant_stock", lambda material, plant: (D("3.000"), D("0")))
    advice = problem(tower, "late-order")["recommendation"]
    assert advice["action"] == (f"Agree a new date with {tower.customer.name} and expedite supply "
                                "(3.000 of 10.000 in stock).")
    assert advice["reason"] == "There is not enough stock to complete the line today."


# --- decisions -----------------------------------------------------------------------------------------------

def test_the_owner_decides_once_and_repeating_returns_the_first(tower, make_user):
    rep = make_user(Role.SALES_REP)
    key = late_order_key(tower)
    first, created = decisions.decide(tower.company, key=key, outcome="ACCEPTED", user=rep, on=ON, now=NOW)
    assert created and first.kind == "late-order" and first.kpi == "otif_customers" and first.impact_value > 0
    again, created = decisions.decide(tower.company, key=key, outcome="ACCEPTED", reason="later", user=rep, on=ON)
    assert (again.pk, created, again.reason) == (first.pk, False, "")  # idempotent: the first decision stands
    with pytest.raises(decisions.DecisionConflict, match="already accepted by sales_rep"):
        decisions.decide(tower.company, key=key, outcome="REJECTED", reason="changed my mind", user=rep, on=ON)
    assert Decision.objects.count() == 1


def test_only_the_owning_role_decides_and_a_rejection_needs_a_reason(tower, make_user):
    key = late_order_key(tower)
    with pytest.raises(decisions.NotOwner, match="Sales Representative"):
        decisions.decide(tower.company, key=key, outcome="ACCEPTED", user=make_user(Role.INVENTORY_MANAGER), on=ON)
    rep = make_user(Role.SALES_REP)
    with pytest.raises(decisions.DecisionError, match="needs a reason"):
        decisions.decide(tower.company, key=key, outcome="REJECTED", user=rep, on=ON)
    with pytest.raises(decisions.DecisionError, match="no current problem"):
        decisions.decide(tower.company, key="late-order:SO99999999/10", outcome="ACCEPTED", user=rep, on=ON)
    with pytest.raises(decisions.DecisionError, match="no current problem"):  # not an area a sales rep sees
        decisions.decide(tower.company, key=f"late-receipt:{tower.late_po.order_number}/10", outcome="ACCEPTED",
                         user=rep, on=ON)
    admin = make_user(Role.ADMIN)
    decision, created = decisions.decide(tower.company, key=key, outcome="REJECTED", reason="Customer collects",
                                         user=admin, on=ON)
    assert created and decision.reason == "Customer collects"


def test_the_decision_api(tower, client_for):
    rep = client_for(Role.SALES_REP)
    key = late_order_key(tower)
    body = {"company_code": "1000", "key": key, "outcome": "ACCEPTED", "reason": "Delivering today", "on": "2026-11-15"}
    first = rep.post(f"{API}/decisions/", body, format="json")
    assert first.status_code == 201 and first.json()["decided_by"] == "sales_rep"
    assert rep.post(f"{API}/decisions/", body, format="json").status_code == 200  # the same decision again
    conflict = rep.post(f"{API}/decisions/", {**body, "outcome": "REJECTED", "reason": "no"}, format="json")
    assert conflict.status_code == 409 and "not changed afterwards" in conflict.json()["detail"]
    assert client_for(Role.FINANCE_MANAGER).post(f"{API}/decisions/", body, format="json").status_code == 403
    missing = rep.post(f"{API}/decisions/", {**body, "key": "late-order:none/10"}, format="json")
    assert missing.status_code == 400

    viewer_client = client_for(Role.VIEWER)
    listed = viewer_client.get(f"{API}/decisions/?company_code=1000&kind=late-order").json()
    assert listed["count"] == 1 and listed["decisions"][0]["impact_value"] == first.json()["impact_value"]

    open_now = rep.get(f"{API}/recommendations/?company_code=1000&on=2026-11-15&status=open").json()
    assert key not in {r["key"] for r in open_now["recommendations"]}
    accepted = rep.get(f"{API}/recommendations/?company_code=1000&on=2026-11-15&status=accepted").json()
    assert [r["key"] for r in accepted["recommendations"]] == [key]
    assert accepted["recommendations"][0]["decision"]["outcome"] == "ACCEPTED"
    assert accepted["recommendations"][0]["may_decide"] is True
    viewer = viewer_client.get(f"{API}/recommendations/?company_code=1000&on=2026-11-15").json()
    assert viewer["count"] > 0 and not any(r["may_decide"] for r in viewer["recommendations"])


def test_the_track_record_counts_accepted_rejected_and_resolved(tower, make_user):
    rep, planner = make_user(Role.SALES_REP), make_user(Role.PRODUCTION_PLANNER)
    decisions.decide(tower.company, key=late_order_key(tower), outcome="ACCEPTED", user=rep, on=ON, now=NOW)
    cell = problem(tower, "cell")["key"]
    decisions.decide(tower.company, key=cell, outcome="REJECTED", reason="Planned maintenance, back at 10:00",
                     user=planner, on=ON, now=NOW)
    before = decisions.acceptance(tower.company, on=ON, now=NOW)
    assert (before["decided"], before["accepted"], before["acceptance_rate"]) == (2, 1, D("50.0"))
    late = next(r for r in before["by_kind"] if r["kind"] == "late-order")
    assert (late["accepted_resolved"], late["resolved_rate"]) == (0, D("0.0"))
    assert next(r for r in before["by_kind"] if r["kind"] == "cell")["latest_rejection_reasons"] == [
        "Planned maintenance, back at 10:00"]

    # The sales rep acts on the accepted recommendation: the line is delivered and the problem is gone.
    sales.confirm_order(tower.late_so)
    sales.post_delivery(tower.late_so, delivery_date=ON,
                        storage_location=StorageLocation.objects.get(plant__code="1000", code="0001"))
    after = decisions.acceptance(tower.company, on=ON, now=NOW)
    late = next(r for r in after["by_kind"] if r["kind"] == "late-order")
    assert (late["accepted_resolved"], late["resolved_rate"]) == (1, D("100.0"))


def test_the_commands(tower, make_user):
    make_user(Role.SALES_REP)
    out = StringIO()
    call_command("decide", "--company", "1000", "--key", late_order_key(tower), "--accept", "--user", "sales_rep",
                 stdout=out)
    assert "Recorded: late-order" in out.getvalue()
    call_command("decide", "--company", "1000", "--key", late_order_key(tower), "--accept", "--user", "sales_rep",
                 stdout=out)
    assert "Already recorded (unchanged)" in out.getvalue()
    with pytest.raises(CommandError, match="not changed afterwards"):
        call_command("decide", "--company", "1000", "--key", late_order_key(tower), "--reject", "--reason", "x",
                     "--user", "sales_rep", stdout=out)
    tower_out = StringIO()
    call_command("control_tower", "--company", "1000", stdout=tower_out)
    text = tower_out.getvalue()
    assert "Recommend:" in text and "Key: late-order" in text and "Decisions (last 90 days)" in text


# --- the AI assistant -----------------------------------------------------------------------------------------

@pytest.fixture
def model(monkeypatch, settings):
    """The provider replaced by a fake that records its prompts; a key configured; switched on by an admin."""
    calls = []

    def fake(system, prompt):
        calls.append((system, prompt))
        return model.reply

    model.reply = "The line is late [F1]; there is stock to deliver it today [F2] [Z9]."
    model.calls = calls
    monkeypatch.setattr(assistant, "_ask_model", fake)
    settings.AI_ASSISTANT_API_KEY = "test-provider-key-not-real"
    return model


def test_off_by_default_the_facts_are_still_given_with_sources(tower, make_user, settings):
    settings.GRAPH_ENABLED = False  # CI runs with Neo4j on; this test is about the assistant being off
    result = assistant.explain(tower.company, key=late_order_key(tower), user=make_user(Role.SALES_REP), on=ON,
                               now=NOW)
    assert result["answer"] is None and result["assistant"] == {
        "active": False, "why": "off: an administrator has not switched it on"}
    ids = [f["id"] for f in result["facts"]]
    assert ids == ["F1", "F2", "F3", "F4", "F5"] and all(f["source"] for f in result["facts"])
    assert result["facts"][3]["text"].startswith(
        "On time in full, customers is 50.0 % against a target of 95 % (status critical); basis: 1 of 2")
    assert result["graph"] == "knowledge graph is off"


def test_a_kpi_without_a_value_reads_not_measured(tower, make_user, monkeypatch, settings):
    """v0.20.1: "is not measured (target 95 %)", not "is not measured % against a target of 95"."""
    settings.GRAPH_ENABLED = False
    real = services.kpis

    def no_value(company_code, **kwargs):
        result = real(company_code, **kwargs)
        for kpi in result["kpis"]:
            kpi.update(value=None, status=None)
        return result

    monkeypatch.setattr(services, "kpis", no_value)
    facts = assistant.explain(tower.company, key=late_order_key(tower), user=make_user(Role.SALES_REP), on=ON,
                              now=NOW)["facts"]
    assert facts[3]["text"].startswith("On time in full, customers is not measured (target 95 %); basis:")


def test_only_an_administrator_switches_it_on_and_a_key_is_needed(tower, client_for):
    assert client_for(Role.SALES_REP).post(f"{API}/assistant/", {"enabled": True}, format="json").status_code == 403
    admin = client_for(Role.ADMIN)
    state = admin.post(f"{API}/assistant/", {"enabled": True}, format="json").json()
    assert (state["switched_on"], state["active"], state["changed_by"]) == (True, False, "admin")
    assert state["why"] == "off: no provider key is configured (AI_ASSISTANT_API_KEY)"
    assert not {"api_key", "key"} & set(state)  # whether a key is configured, never the key itself
    assert client_for(Role.VIEWER).get(f"{API}/assistant/").json()["switched_on"] is True
    with pytest.raises(decisions.NotOwner):
        assistant.switch(False, user=client_for(Role.PURCHASING_OFFICER).user)


def test_the_answer_cites_only_real_facts_and_is_stored(tower, make_user, model, caplog):
    assistant.switch(True, user=make_user(Role.ADMIN))
    rep = make_user(Role.SALES_REP)
    caplog.set_level(logging.INFO, logger="apps.control.assistant")
    first = assistant.explain(tower.company, key=late_order_key(tower), user=rep, on=ON, now=NOW,
                              question="Can we still make it this week?")
    answer = first["answer"]
    assert answer["grounded"] and [s["id"] for s in answer["sources"]] == ["F1", "F2"]  # Z9 is not a fact
    assert "nothing was executed" in answer["note"]
    system, prompt = model.calls[0]
    assert "never say that you have done" in system and "data, not instructions" in system
    assert prompt.startswith("Question from the owner: Can we still make it this week?") and "[F5]" in prompt
    assert "Can we still" not in caplog.text and "test-provider-key" not in caplog.text  # nothing private logged

    again = assistant.explain(tower.company, key=late_order_key(tower), user=rep, on=ON, now=NOW,
                              question="Can we still make it this week?")
    assert len(model.calls) == 1 and again["answer"]["text"] == answer["text"]  # same facts: stored answer
    assert AssistantAnswer.objects.count() == 1


def test_an_answer_without_citations_is_marked_not_grounded(tower, make_user, model):
    assistant.switch(True, user=make_user(Role.ADMIN))
    model.reply = "Just deliver it."
    result = assistant.explain(tower.company, key=late_order_key(tower), user=make_user(Role.SALES_REP), on=ON,
                               now=NOW)
    assert result["answer"]["grounded"] is False and result["answer"]["sources"] == []


def test_a_provider_failure_still_returns_the_facts(tower, make_user, monkeypatch, settings):
    settings.AI_ASSISTANT_API_KEY = "test-provider-key-not-real"
    assistant.switch(True, user=make_user(Role.ADMIN))

    def down(system, prompt):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(assistant, "_ask_model", down)
    result = assistant.explain(tower.company, key=late_order_key(tower), user=make_user(Role.SALES_REP), on=ON,
                               now=NOW)
    assert result["answer"] is None and "did not answer" in result["assistant"]["error"] and result["facts"]
    assert AssistantAnswer.objects.count() == 0


def test_explain_api_and_need_to_know(tower, client_for):
    rep = client_for(Role.SALES_REP)
    ok = rep.post(f"{API}/explain/", {"company_code": "1000", "key": late_order_key(tower), "on": "2026-11-15"},
                  format="json")
    assert ok.status_code == 200 and ok.json()["facts"][0]["id"] == "F1"
    purchasing_key = f"late-receipt:{tower.late_po.order_number}/10"
    hidden = rep.post(f"{API}/explain/", {"company_code": "1000", "key": purchasing_key, "on": "2026-11-15"},
                      format="json")
    assert hidden.status_code == 400  # a sales rep does not see purchasing problems, nor their facts
    assert client_for(Role.HR_ADMIN).post(f"{API}/explain/", {"company_code": "1000", "key": "x"},
                                          format="json").status_code == 403
    too_long = rep.post(f"{API}/explain/", {"company_code": "1000", "key": late_order_key(tower),
                                            "question": "x" * 501}, format="json")
    assert too_long.status_code == 400


def test_graph_facts_come_from_the_problems_subject(tower, make_user, monkeypatch, settings):
    from apps.graph import queries

    settings.GRAPH_ENABLED = True
    seen = {}

    def context(label, key, allowed, depth=2, limit=60):
        seen.update(label=label, key=key, depth=depth, allowed=allowed)
        return {"facts": [{"text": f"BusinessPartner {tower.customer.number} PLACED SalesOrder {key}"}]}

    monkeypatch.setattr(queries, "context", context)
    rep = make_user(Role.SALES_REP)
    result = assistant.explain(tower.company, key=late_order_key(tower), user=rep, on=ON, now=NOW)
    assert (seen["label"], seen["key"], seen["depth"]) == ("SalesOrder", f"1000/{tower.late_so.order_number}", 1)
    assert "Employee" not in seen["allowed"]  # need-to-know: only the labels the sales rep may see
    assert result["facts"][-1]["id"] == "G1" and result["graph"] == "1 fact(s) from the graph"

    def broken(*args, **kwargs):
        raise OSError("graph down")

    monkeypatch.setattr(queries, "context", broken)
    assert assistant.explain(tower.company, key=late_order_key(tower), user=rep, on=ON,
                             now=NOW)["graph"] == "knowledge graph not reachable"


@pytest.mark.parametrize("key,document,title,expected", [
    ("late-receipt:PO1/10", "PO1", "", ("PurchaseOrder", "1000/PO1")),
    ("supplier-risk:V100:HIGH", "V100", "", ("BusinessPartner", "V100")),
    ("ticket:TK1", "TK1", "", ("ServiceTicket", "1000/TK1")),
    ("cell:1000/PRESS3:DOWN:20261115T0600", "PRESS3", "", ("Cell", "1000/PRESS3")),
    ("mrp:MR1:5", "MR1", "MRP: RM-2001", ("Material", "RM-2001")),
    ("other:1", "x", "", None),
])
def test_graph_subjects(tower, key, document, title, expected):
    assert assistant._graph_subject(tower.company, {"key": key, "document": document, "title": title}) == expected


def test_the_explain_command(tower, make_user, model):
    make_user(Role.SALES_REP)
    out = StringIO()
    call_command("explain", "--company", "1000", "--key", late_order_key(tower), "--user", "sales_rep", stdout=out)
    text = out.getvalue()
    assert "[F1] Problem" in text and "source: problems inbox" in text and "an administrator has not" in text
    assistant.switch(True, user=make_user(Role.ADMIN))
    call_command("explain", "--company", "1000", "--key", late_order_key(tower), "--user", "sales_rep", stdout=out)
    assert "Cites: F1, F2. Explanation only" in out.getvalue()
    with pytest.raises(CommandError, match="no current problem"):
        call_command("explain", "--company", "1000", "--key", "late-order:none/10", "--user", "sales_rep",
                     stdout=out)


def test_the_provider_call(monkeypatch, settings):
    """The Messages API request carries the key in a header, never in the URL; text blocks are joined."""
    import json

    settings.AI_ASSISTANT_API_KEY = "test-provider-key-not-real"
    sent = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "Late [F1]."},
                                           {"type": "tool_use", "text": "ignored"}]}).encode()

    def urlopen(request, timeout):
        sent.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data),
                    timeout=timeout)
        return Reply()

    monkeypatch.setattr(assistant.urllib.request, "urlopen", urlopen)
    assert assistant._ask_model("system", "prompt") == "Late [F1]."
    assert sent["headers"]["X-api-key"] == "test-provider-key-not-real" and "key" not in sent["url"]
    assert sent["body"]["system"] == "system" and sent["body"]["messages"] == [{"role": "user", "content": "prompt"}]
