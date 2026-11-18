"""MRP (Wave 8b): the pure planner, the snapshot from real documents, runs, conversion, API and roles.

The demo product (``setup_demo_production``): FG-4001 = 1 x SF-3001 + 4 x RM-2002, SF-3001 = 2.5 x
RM-2001; opening stock 1,000 x RM-2001 and 2,000 x RM-2002. Planned on 14 November 2026 for a sales
order of 450 x FG-4001 wanted on 15 December, with the demo planning parameters (``setup_demo_mrp``).
"""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization
from apps.planning import mrp
from apps.planning.models import (ForecastItem, ForecastPeriod, ForecastRun, MaterialPlanning, MRPRun,
                                  PlannedOrder)
from apps.production.models import ProductionOrder
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder
from apps.sales import services as sales
from apps.srm import services as srm

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/planning"
TODAY = date(2026, 11, 14)
LOT = MaterialPlanning.LotSize


# --- the pure planner ---------------------------------------------------------------------------------

def material(stock="0", safety="0", lot_size="EX", lot_quantity=None, lead=5, procurement="BUY", **events):
    """A snapshot entry with the given parameters and events (lists of [date, quantity])."""
    return {"procurement": procurement, "lot_size": lot_size, "lot_quantity": lot_quantity, "safety_stock": safety,
            "lead_time_days": lead, "stock": stock, "has_bom": procurement == "MAKE",
            "independent": events.get("independent", []), "receipts": events.get("receipts", []),
            "dependent": events.get("dependent", [])}


def snap(materials, boms=None):
    return {"plant": "1000", "planning_date": "2026-11-14", "months": ["2026-11", "2026-12", "2027-01"],
            "materials": materials, "boms": boms or {}}


def test_lot_sizes():
    assert mrp.lot(D("7"), LOT.LOT_FOR_LOT, None) == D("7.000")
    assert mrp.lot(D("7"), LOT.FIXED, D("50")) == D("50.000") and mrp.lot(D("101"), LOT.FIXED, D("50")) == D("150.000")
    assert mrp.lot(D("7"), LOT.MINIMUM, D("500")) == D("500.000") and mrp.lot(D("750"), LOT.MINIMUM, D("500")) == D(
        "750.000")


def test_low_level_codes_put_components_after_their_parents():
    boms = {"FG": {"base": "1", "items": [["SF", "1"], ["RM2", "4"]]}, "SF": {"base": "1", "items": [["RM1", "2.5"]]},
            "OTHER": {"base": "1", "items": [["RM2", "1"]]}}
    assert mrp.low_level_codes(["FG", "SF", "RM1", "RM2", "OTHER"], boms) == {
        "FG": 0, "SF": 1, "RM1": 2, "RM2": 1, "OTHER": 0}


def test_netting_receipts_safety_stock_and_lead_time():
    result = mrp.plan(snap({"X": material(stock="100", safety="20", lead=10,
                                          independent=[["2026-12-01", "60"], ["2027-01-10", "80"]],
                                          receipts=[["2027-01-05", "30"]])}))
    # 100 - 60 = 40 (above safety); 40 + 30 - 80 = -10: short by 30 to reach the safety stock on 10 January.
    [order] = result.proposals
    assert (order.quantity, order.due_date, order.start_date, order.is_late) == (
        D("30.000"), date(2027, 1, 10), date(2026, 12, 31), False)
    lines = {f"{line['period']:%Y-%m}": line for line in result.lines["X"]}
    assert lines["2026-11"]["available"] == D("100.000") and lines["2026-12"]["available"] == D("40.000")
    assert (lines["2027-01"]["receipts"], lines["2027-01"]["planned"], lines["2027-01"]["available"]) == (
        D("30.000"), D("30.000"), D("20.000"))


def test_short_today_means_a_late_order_and_a_message():
    result = mrp.plan(snap({"X": material(stock="5", safety="20", lead=3)}))
    [order] = result.proposals
    assert (order.quantity, order.due_date, order.is_late) == (D("15.000"), TODAY, True)
    assert "should have started on 2026-11-11" in result.messages[0][1]


def test_make_orders_explode_into_component_requirements_on_their_start_date():
    materials = {"FG": material(procurement="MAKE", lot_size="FX", lot_quantity="50", lead=5,
                                independent=[["2026-12-15", "120"]]),
                 "RM": material(stock="200", lead=7)}
    result = mrp.plan(snap(materials, {"FG": {"base": "10", "items": [["RM", "25"]]}}))  # 2.5 RM per FG
    fg, rm = result.proposals
    assert (fg.material, fg.quantity, fg.start_date) == ("FG", D("150.000"), date(2026, 12, 10))
    # 150 x 2.5 = 375 needed on 10 December; 200 in stock: 175 to buy, due that day, ordered 7 days before.
    assert (rm.material, rm.quantity, rm.due_date, rm.start_date) == ("RM", D("175.000"), date(2026, 12, 10),
                                                                      date(2026, 12, 3))
    assert next(line for line in result.lines["RM"] if line["period"] == date(2026, 12, 1))["dependent"] == D("375.000")


def test_make_without_a_bom_is_reported():
    result = mrp.plan(snap({"FG": {**material(procurement="MAKE"), "has_bom": False}}))
    assert "no active BOM" in result.messages[0][1]


# --- from real documents --------------------------------------------------------------------------------

@pytest.fixture
def plant(demo):
    call_command("setup_demo_production", stdout=StringIO())
    plant = Plant.objects.get(code="1000")
    for number, procurement, lot_size, lot_quantity, safety, lead in (
            ("FG-4001", "MAKE", LOT.FIXED, D("50"), D("10"), 5), ("SF-3001", "MAKE", LOT.LOT_FOR_LOT, None, 0, 3),
            ("RM-2001", "BUY", LOT.MINIMUM, D("500"), 0, 10), ("RM-2002", "BUY", LOT.LOT_FOR_LOT, None, D("200"), 14)):
        MaterialPlanning.objects.create(material=Material.objects.get(number=number), plant=plant,
                                        procurement=procurement, lot_size=lot_size, lot_quantity=lot_quantity,
                                        safety_stock=safety, lead_time_days=lead,
                                        preferred_supplier=demo.supplier if number == "RM-2001" else None)
    sales.create_sales_order(sales_organization=SalesOrganization.objects.get(code="1000"), customer=demo.customer,
                             order_date=TODAY, requested_delivery_date=date(2026, 12, 15),
                             items=[sales.OrderItemInput(material=Material.objects.get(number="FG-4001"),
                                                         quantity=D("450"), plant=plant)])
    return plant


def proposals(run):
    return [(o.procurement, o.material.number, o.quantity, o.due_date, o.is_late)
            for o in run.planned_orders.select_related("material")]


def test_mrp_plans_the_demo_product(plant):
    run, created = mrp.run_mrp(plant, planning_date=TODAY)
    assert created and run.run_number == "MR00000001" and run.first_period == date(2026, 11, 1)
    assert proposals(run) == [
        ("MAKE", "FG-4001", D("50.000"), TODAY, True),  # the safety stock of 10, in lots of 50, now
        ("MAKE", "FG-4001", D("450.000"), date(2026, 12, 15), False),  # the sales order
        ("BUY", "RM-2002", D("200.000"), date(2026, 12, 10), False),  # 4 x 500 = 2,000: keep the 200 safety stock
        ("MAKE", "SF-3001", D("50.000"), TODAY, True),
        ("MAKE", "SF-3001", D("450.000"), date(2026, 12, 10), False),  # when the bracket order starts
        ("BUY", "RM-2001", D("500.000"), date(2026, 12, 7), False),  # 1,250 kg needed, 1,000 in stock, min lot
    ]
    levels = {m.material.number: m.low_level_code for m in run.materials.select_related("material")}
    assert levels == {"FG-4001": 0, "SF-3001": 1, "RM-2002": 1, "RM-2001": 2}
    assert run.messages.count() == 2  # the two late orders


def test_unchanged_data_returns_the_same_run_and_new_data_supersedes(plant, demo):
    first, _ = mrp.run_mrp(plant, planning_date=TODAY)
    again, created = mrp.run_mrp(plant, planning_date=TODAY)
    assert again == first and not created and MRPRun.objects.count() == 1
    # A purchase order for steel arrives in the plan: a new run, and the first run's proposals are superseded.
    purchasing.create_purchase_order(
        purchasing_organization=PurchasingOrganization.objects.get(code="1000"), supplier=demo.supplier,
        order_date=TODAY, delivery_date=date(2026, 12, 1),
        items=[purchasing.OrderItemInput(material=Material.objects.get(number="RM-2001"), quantity=D("300"),
                                         net_price=D("38"), plant=plant)])
    second, created = mrp.run_mrp(plant, planning_date=TODAY)
    assert created and second.run_number == "MR00000002"
    assert set(first.planned_orders.values_list("status", flat=True)) == {"SUPERSEDED"}
    steel = [p for p in proposals(second) if p[1] == "RM-2001"]
    assert steel == []  # 1,000 + 300 covers the 1,250 kg


def test_the_forecast_is_consumed_by_orders(plant):
    run = ForecastRun.objects.create(company_code=plant.company_code, plant=plant, run_number="FR00000001",
                                     first_period=date(2026, 11, 1), horizon=2, history_months=12,
                                     model_version="0.1.0", fingerprint="x")
    item = ForecastItem.objects.create(run=run, material=Material.objects.get(number="FG-4001"), method="naive",
                                       history_total=D("0"))
    for month, quantity in ((date(2026, 11, 1), "100"), (date(2026, 12, 1), "300")):
        ForecastPeriod.objects.create(item=item, period=month, quantity=D(quantity), lower=0, upper=D(quantity))
    snap_ = mrp.snapshot(plant, TODAY, 3)
    # November: the forecast of 100, counted today; December: the order of 450 covers the forecast of 300.
    assert snap_["materials"]["FG-4001"]["independent"] == [["2026-11-14", "100.000"], ["2026-12-15", "450.000"]]


def test_open_production_orders_are_receipts_and_their_components_requirements(plant):
    from apps.production import orders

    orders.create_order(plant=plant, material=Material.objects.get(number="SF-3001"), quantity=D("40"),
                        start_date=date(2026, 11, 20), finish_date=date(2026, 11, 25))
    snap_ = mrp.snapshot(plant, TODAY, 3)
    assert snap_["materials"]["SF-3001"]["receipts"] == [["2026-11-25", "40.000"]]
    assert snap_["materials"]["RM-2001"]["dependent"] == [["2026-11-20", "100.000"]]


def test_mrp_rules(demo):
    with pytest.raises(mrp.MRPError, match="nothing to plan"):
        mrp.run_mrp(Plant.objects.get(code="1000"), planning_date=TODAY)
    with pytest.raises(mrp.MRPError, match="1 to 12 months"):
        mrp.run_mrp(Plant.objects.get(code="1000"), planning_date=TODAY, horizon=13)


# --- converting planned orders ---------------------------------------------------------------------------

def planned(run, number):
    return run.planned_orders.get(material__number=number, due_date__gt=TODAY)


def test_convert_make_creates_a_production_order_once(plant):
    run, _ = mrp.run_mrp(plant, planning_date=TODAY)
    order = planned(run, "FG-4001")
    number = mrp.convert(order, on=TODAY)
    assert number == mrp.convert(order, on=TODAY)  # again: the same production order
    production = ProductionOrder.objects.get(order_number=number)
    assert (production.quantity, production.start_date, production.finish_date) == (
        D("450.000"), date(2026, 12, 10), date(2026, 12, 15))
    order.refresh_from_db()
    assert (order.status, order.document_number) == ("CONVERTED", number)


def test_convert_buy_uses_a_contract_then_the_preferred_supplier(plant, demo):
    srm.create_contract(purchasing_organization=PurchasingOrganization.objects.get(code="1000"),
                        supplier=demo.supplier, valid_from=date(2026, 11, 1), valid_to=date(2027, 2, 28),
                        items=[srm.ContractItemInput(Material.objects.get(number="RM-2002"), D("1.40"), D("5000"))])
    run, _ = mrp.run_mrp(plant, planning_date=TODAY)
    fasteners = mrp.convert(planned(run, "RM-2002"), on=TODAY)
    order = PurchaseOrder.objects.get(order_number=fasteners)
    assert order.supplier_reference == "CT00000001" and order.items.get().net_price == D("1.40")  # a release

    steel = planned(run, "RM-2001")
    with pytest.raises(mrp.MRPError, match="give the net price"):
        mrp.convert(steel, on=TODAY)  # Highveld is preferred but has never been asked for steel
    number = mrp.convert(steel, on=TODAY, net_price=D("38.50"))
    purchase = PurchaseOrder.objects.get(order_number=number)
    assert (purchase.supplier, purchase.delivery_date, purchase.items.get().quantity) == (
        demo.supplier, date(2026, 12, 7), D("500.000"))


def test_superseded_planned_orders_cannot_be_converted(plant):
    run, _ = mrp.run_mrp(plant, planning_date=TODAY)
    PlannedOrder.objects.filter(run=run).update(status="SUPERSEDED")
    with pytest.raises(mrp.MRPError, match="latest MRP run"):
        mrp.convert(planned(run, "FG-4001"), on=TODAY)


# --- API and roles ---------------------------------------------------------------------------------------

def test_planners_run_mrp_and_owners_convert(plant, client_for):
    planner = client_for(Role.PRODUCTION_PLANNER)
    created = planner.post(f"{API}/mrp-runs/", {"plant": "1000", "planning_date": "2026-11-14"}, format="json")
    assert created.status_code == 201, created.data
    data = created.json()
    assert data["run_number"] == "MR00000001" and len(data["planned_orders"]) == 6 and len(data["messages"]) == 2
    fg = next(m for m in data["materials"] if m["material"] == "FG-4001")
    assert fg["lines"][1] == {"period": "2026-12", "independent": "450.000", "dependent": "0.000",
                              "receipts": "0.000", "planned": "450.000", "available": "50.000"}
    again = planner.post(f"{API}/mrp-runs/", {"plant": "1000", "planning_date": "2026-11-14"}, format="json")
    assert again.status_code == 200 and again.json()["run_number"] == "MR00000001"

    listed = planner.get(f"{API}/planned-orders/?plant=1000&status=OPEN&procurement=BUY").json()["results"]
    assert [o["material"] for o in listed] == ["RM-2002", "RM-2001"]
    buy_id = listed[1]["id"]
    make_id = next(o["id"] for o in data["planned_orders"] if o["material"] == "FG-4001" and not o["is_late"])

    converted = planner.post(f"{API}/planned-orders/{make_id}/convert/", {"on": "2026-11-14"}, format="json")
    assert converted.status_code == 200 and converted.json()["document_number"].startswith("MO")
    refused = planner.post(f"{API}/planned-orders/{buy_id}/convert/", {}, format="json")
    assert refused.status_code == 403 and "Purchasing Officer" in refused.data["detail"]
    buyer = client_for(Role.PURCHASING_OFFICER)
    bought = buyer.post(f"{API}/planned-orders/{buy_id}/convert/", {"on": "2026-11-14", "net_price": "38.50"},
                        format="json")
    assert bought.status_code == 200 and bought.json()["status"] == "CONVERTED"
    no_price = buyer.post(f"{API}/planned-orders/{listed[0]['id']}/convert/", {"on": "2026-11-14"}, format="json")
    assert no_price.status_code == 400 and "name the supplier" in no_price.data["non_field_errors"][0]


def test_planning_parameters_and_roles(plant, client_for):
    planner = client_for(Role.PRODUCTION_PLANNER)
    bad = planner.post(f"{API}/material-planning/", {"material": "TG-1001", "plant": "1000", "procurement": "BUY",
                                                     "lot_size": "FX", "lead_time_days": 7}, format="json")
    assert bad.status_code == 400 and "lot quantity" in str(bad.data["lot_quantity"])
    good = planner.post(f"{API}/material-planning/", {"material": "TG-1001", "plant": "1000", "procurement": "BUY",
                                                      "lot_size": "FX", "lot_quantity": "25", "lead_time_days": 7},
                        format="json")
    assert good.status_code == 201
    assert client_for(Role.VIEWER).get(f"{API}/mrp-runs/").status_code == 200
    assert client_for(Role.SALES_REP).post(f"{API}/mrp-runs/", {"plant": "1000"}, format="json").status_code == 403
    assert client_for(Role.HR_ADMIN).get(f"{API}/planned-orders/").status_code == 403


def test_demo_command_and_mrp_command(demo):
    call_command("setup_demo_production", stdout=StringIO())
    out = StringIO()
    call_command("setup_demo_mrp", stdout=out)
    call_command("setup_demo_mrp", stdout=out)
    assert "planning parameters for 4 material(s)" in out.getvalue() and "nothing to do" in out.getvalue()
    assert BusinessPartner.objects.get(name="Highveld Supplies CC") == MaterialPlanning.objects.get(
        material__number="RM-2001").preferred_supplier
    call_command("run_mrp", "--plant", "1000", "--date", "2026-11-14", stdout=out)
    call_command("run_mrp", "--plant", "1000", "--date", "2026-11-14", stdout=out)
    assert "Created MRP run MR00000001" in out.getvalue() and "Unchanged MRP run MR00000001" in out.getvalue()


def test_the_nightly_task(plant):
    from apps.planning import tasks

    first = tasks.mrp_all_plants()
    assert first["created"] == 1 and tasks.mrp_all_plants()["unchanged"] == 1


# --- v0.20.1: messages point at their planned orders; the control tower proposes converting them ------------

def test_the_planner_records_which_proposal_a_late_message_is_about():
    result = mrp.plan(snap({"X": material(stock="5", safety="20", lead=3)}))
    assert result.message_orders == {0: 1}  # message 0 is about proposal 1


def test_late_messages_point_at_their_planned_orders(plant):
    run, _ = mrp.run_mrp(plant, planning_date=TODAY)
    messages = run.messages.select_related("material", "planned_order")
    linked = {(m.material.number, m.planned_order.number) for m in messages}
    assert linked == {("FG-4001", 1), ("SF-3001", 4)}  # the two late MAKE orders of the demo plan


def test_mrp_recommendations_name_the_planned_order_and_its_owner(plant, demo, make_user):
    from apps.control import decisions, services

    MaterialPlanning.objects.filter(material__number="RM-2002").update(safety_stock=D("2500"))  # short today: BUY
    run, _ = mrp.run_mrp(plant, planning_date=TODAY)
    found = {p["title"]: p for p in services.problems(demo.company, on=TODAY) if p["area"] == "planning"}
    make, buy = found["MRP: FG-4001"], found["MRP: RM-2002"]
    first = run.planned_orders.get(number=1)
    assert make["recommendation"]["action"] == (
        "Convert planned order MR00000001/1 (50.000 x FG-4001, due 2026-11-14) into a production order and "
        f"release it today; or move the demand it covers. API: POST /api/planning/planned-orders/{first.pk}/convert/")
    assert make["owner_role"] == Role.PRODUCTION_PLANNER and "should have started on 2026-11-09" in \
        make["recommendation"]["reason"]
    assert buy["owner_role"] == Role.PURCHASING_OFFICER and "into a purchase order now" in \
        buy["recommendation"]["action"]
    decision, created = decisions.decide(demo.company, key=buy["key"], outcome="ACCEPTED",
                                         user=make_user(Role.PURCHASING_OFFICER), on=TODAY)
    assert created and decision.title == "MRP: RM-2002"

    number = mrp.convert(first, on=TODAY)  # the planner acts on the recommendation
    again = next(p for p in services.problems(demo.company, on=TODAY) if p["key"] == make["key"])
    assert again["recommendation"]["action"] == f"Planned order MR00000001/1 is already {number}: expedite that order."


def test_runs_stored_before_v0_20_1_are_linked_by_the_migration(plant):
    import importlib

    from django.apps import apps

    run, _ = mrp.run_mrp(plant, planning_date=TODAY)
    run.messages.update(planned_order=None)  # as stored by v0.20.0
    step = importlib.import_module("apps.planning.migrations.0003_message_planned_order")
    step.link_existing(apps, None)
    step.link_existing(apps, None)  # idempotent: nothing changes the second time
    linked = sorted((m.material.number, m.planned_order.number) for m in run.messages.select_related(
        "material", "planned_order"))
    assert linked == [("FG-4001", 1), ("SF-3001", 4)]


def test_a_decision_on_an_mrp_problem_survives_the_next_run(plant, demo, make_user):
    """v0.21.1: MRP problems are keyed by plant, material and due date, not by the run that found them."""
    from apps.control import decisions, services

    first, _ = mrp.run_mrp(plant, planning_date=TODAY)
    late = next(p for p in services.problems(demo.company, on=TODAY) if p["title"] == "MRP: FG-4001")
    assert late["key"] == "mrp:1000/FG-4001:20261114"
    decisions.decide(demo.company, key=late["key"], outcome="ACCEPTED", user=make_user(Role.PRODUCTION_PLANNER),
                     on=TODAY)
    MaterialPlanning.objects.filter(material__number="RM-2002").update(safety_stock=D("250"))  # unrelated change
    second, created = mrp.run_mrp(plant, planning_date=TODAY)
    assert created and second.run_number != first.run_number  # MRP replanned
    again = next(r for r in decisions.recommendations(demo.company, on=TODAY) if r["title"] == "MRP: FG-4001")
    assert again["key"] == late["key"] and again["decision"]["outcome"] == "ACCEPTED"
    keys = [p["key"] for p in services.problems(demo.company, on=TODAY)]
    assert len(keys) == len(set(keys))
