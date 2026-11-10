"""Production orders (Wave 5b): the order as a cost object, from creation to settlement.

Two orders in plant 1000, worked out by hand:

* MO00000001: 100 x SF-3001 Pressed frame, planned R16,425.00 (R164.25 each). Steel 250 kg x R38
  = 9,500; machine 10.5 h x R450 = 4,725; labour 10 h x R220 = 2,200. Everything as planned, so
  the order balance after receiving 100 frames is zero: no variance.
* MO00000002: 100 x FG-4001 Assembled bracket, planned R22,525.00 (R225.25 each). It uses 10 more
  fastener packs than planned (+R15.00) and one more labour hour (+R220.00): variance R235.00,
  settled to production variances (5600).
"""
import json
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db.models import Sum

from apps.accounts.models import Role
from apps.finance import services as finance
from apps.finance.models import GLAccount, JournalLine
from apps.graph import access, processor
from apps.graph.store import MemoryStore
from apps.inventory import services as inventory
from apps.inventory.models import MaterialDocument, MovementType
from apps.masterdata.models import Material
from apps.organization.models import Plant
from apps.production import orders
from apps.production.models import ProductionOrder
from apps.production.services import ProductionError

pytestmark = pytest.mark.django_db
D = Decimal
JUNE_1, JUNE_2 = date(2026, 6, 1), date(2026, 6, 2)
API = "/api/production/orders"


@pytest.fixture
def pp(demo):
    call_command("setup_demo_production", stdout=StringIO())
    demo.plant = Plant.objects.get(code="1000")
    demo.material = lambda number: Material.objects.get(number=number)
    return demo


@pytest.fixture
def planner(client_for):
    return client_for(Role.PRODUCTION_PLANNER)


def account_total(demo, number, **filters):
    total = JournalLine.objects.filter(gl_account=demo.account(number), **filters).aggregate(t=Sum("amount_local"))
    return total["t"] or D("0")


def frame_order(pp, quantity="100"):
    order = orders.create_order(plant=pp.plant, material=pp.material("SF-3001"), quantity=D(quantity),
                                start_date=JUNE_1)
    return orders.release_order(order, JUNE_1)


def made_frames(pp):
    """MO00000001: 100 frames made exactly as planned, received and completed."""
    order = frame_order(pp)
    orders.issue_components(order, JUNE_1)
    orders.confirm_operation(order, 10, JUNE_1, yield_quantity=D("100"), final=True)
    orders.receive_output(order, JUNE_1, D("100"))
    return orders.complete_order(order, JUNE_1)


# --- the whole flow through the API ----------------------------------------------------------------

def test_frame_order_as_planned_through_the_api(pp, planner, client_for):
    warehouse = client_for(Role.INVENTORY_MANAGER)
    created = planner.post(f"{API}/", {"material": "SF-3001", "plant": "1000", "quantity": "100",
                                       "start_date": "2026-06-01", "finish_date": "2026-06-03"}, format="json")
    assert created.status_code == 201, created.data
    order = created.data
    assert (order["order_number"], order["status"], order["planned_cost"], order["planned_unit_cost"]) == (
        "MO00000001", "CREATED", "16425.00", "164.2500")
    assert order["components"] == [{
        "item_number": 10, "material": "RM-2001", "required_quantity": "250.000", "issued_quantity": "0.000",
        "storage_location": "0001", "planned_value": "9500.00",
    }]
    [operation] = order["operations"]
    assert (operation["work_center"], operation["uns_path"]) == ("PRESS3", "1000/1000/assy/l1/press3")
    assert [(a["activity_type"], a["planned_quantity"]) for a in operation["activities"]] == [
        ("LAB", "10.000"), ("MACH", "10.500")]

    url = f"{API}/{order['id']}"
    assert planner.post(f"{url}/release/", {"posting_date": "2026-06-01"}, format="json").data["status"] == "RELEASED"
    issued = warehouse.post(f"{url}/issue/", {"posting_date": "2026-06-01"}, format="json")
    assert issued.status_code == 201, issued.data
    confirmed = planner.post(f"{url}/confirmations/", {"posting_date": "2026-06-01", "operation_number": 10,
                                                       "yield_quantity": "100", "final": True}, format="json")
    assert confirmed.status_code == 201, confirmed.data
    assert [(a["activity_type"], a["amount"]) for a in confirmed.data["activities"]] == [
        ("LAB", "2200.00"), ("MACH", "4725.00")]
    received = warehouse.post(f"{url}/receive/", {"posting_date": "2026-06-01", "quantity": "100"}, format="json")
    assert received.status_code == 201, received.data

    costs = planner.get(f"{url}/costs/").data
    assert (costs["actual_cost"], costs["output_credit"], costs["balance"]) == (
        D("16425.00"), D("16425.00"), D("0.00"))
    done = planner.post(f"{url}/complete/", {"posting_date": "2026-06-03"}, format="json")
    assert done.data["status"] == "COMPLETED" and done.data["settlement_document"] is None  # nothing to settle
    assert done.data["delivered_quantity"] == "100.000"
    listed = planner.get(f"{API}/?plant=1000&status=COMPLETED&material=SF-3001")
    assert listed.data["count"] == 1
    assert len(planner.get(f"{url}/confirmations/").data) == 1


def test_bracket_order_with_a_variance_is_settled(pp, planner, client_for):
    made_frames(pp)  # the bracket needs frames in stock
    created = planner.post(f"{API}/", {"material": "FG-4001", "plant": "1000", "quantity": "100",
                                       "start_date": "2026-06-02"}, format="json")
    url = f"{API}/{created.data['id']}"
    assert created.data["planned_cost"] == "22525.00"
    planner.post(f"{url}/release/", {"posting_date": "2026-06-02"}, format="json")
    issued = planner.post(f"{url}/issue/", {"posting_date": "2026-06-02", "items": [
        {"material": "SF-3001", "quantity": "100"},
        {"material": "RM-2002", "quantity": "410", "storage_location": "0001"},
    ]}, format="json")
    assert issued.status_code == 201, issued.data
    confirmation = {"posting_date": "2026-06-02", "operation_number": 10, "yield_quantity": "100",
                    "activities": {"LAB": "26"}, "final": True, "external_id": "mes-evt-0001"}
    assert planner.post(f"{url}/confirmations/", confirmation, format="json").status_code == 201
    # The MES may deliver the same message twice: the repeat is refused, so the work is charged once.
    repeat = planner.post(f"{url}/confirmations/", confirmation, format="json")
    assert repeat.status_code == 400 and "already posted" in str(repeat.data)
    planner.post(f"{url}/receive/", {"posting_date": "2026-06-02", "quantity": "100"}, format="json")

    done = client_for(Role.FINANCE_MANAGER).post(f"{url}/complete/", {"posting_date": "2026-06-02"}, format="json")
    assert done.status_code == 200, done.data
    assert done.data["settlement_document"].startswith("PS")
    costs = planner.get(f"{url}/costs/").data
    assert (costs["actual_cost"], costs["output_credit"], costs["settled_variance"], costs["balance"]) == (
        D("22760.00"), D("22525.00"), D("235.00"), D("0.00"))
    fasteners = next(c for c in costs["components"] if c["material"] == "RM-2002")
    assert (fasteners["actual_quantity"], fasteners["actual_value"], fasteners["planned_value"]) == (
        D("410.000"), D("615.00"), D("600.00"))
    [labour] = costs["activities"]
    assert (labour["actual_quantity"], labour["actual_value"]) == (D("26.000"), D("5720.00"))

    # The books: variance in the P&L, the product in stock at standard cost, the cost centre relieved.
    assert account_total(pp, "5600") == D("235.00")
    assert account_total(pp, "6950", cost_center__code="4000") == D("-12645.00")  # 6,925 frames + 5,720 brackets
    assert account_total(pp, "6950", order_number__startswith="MO") == D("12645.00")
    stock = {row["material"]: row for row in inventory.stock_overview(plant=pp.plant)}
    assert (stock["FG-4001"]["quantity"], stock["FG-4001"]["value"]) == (D("100.000"), D("22525.00"))
    assert stock["SF-3001"]["quantity"] == D("0.000")
    assert JournalLine.objects.aggregate(t=Sum("amount_local"))["t"] == 0  # every entry balances


# --- rules -----------------------------------------------------------------------------------------

def test_order_creation_rules(pp):
    def create(material="SF-3001", quantity="10", start=JUNE_1, finish=None):
        return orders.create_order(plant=pp.plant, material=pp.material(material), quantity=D(quantity),
                                   start_date=start, finish_date=finish)

    with pytest.raises(ProductionError, match="greater than zero"):
        create(quantity="0")
    with pytest.raises(ProductionError, match="finish date"):
        create(finish=date(2026, 5, 31))
    with pytest.raises(ProductionError, match="No active BOM"):
        create(material="RM-2001")
    with pytest.raises(ProductionError, match="No active BOM"):
        create(start=date(2026, 2, 1))  # before the BOM was valid


def test_only_released_orders_take_postings(pp):
    order = orders.create_order(plant=pp.plant, material=pp.material("SF-3001"), quantity=D("10"), start_date=JUNE_1)
    with pytest.raises(ProductionError, match="only released orders"):
        orders.issue_components(order, JUNE_1)
    orders.release_order(order, JUNE_1)
    with pytest.raises(ProductionError, match="not created"):
        orders.release_order(order, JUNE_1)
    orders.complete_order(order, JUNE_1)
    with pytest.raises(ProductionError, match="only released orders"):
        orders.receive_output(order, JUNE_1, D("1"))


def test_goods_movement_rules(pp):
    order = frame_order(pp, "10")
    with pytest.raises(ProductionError, match="not a component"):
        orders.issue_components(order, JUNE_1, [orders.IssueInput(pp.material("RM-2002"), D("1"))])
    orders.issue_components(order, JUNE_1)
    with pytest.raises(ProductionError, match="already been issued"):
        orders.issue_components(order, JUNE_1)
    with pytest.raises(ProductionError, match="over-delivery"):
        orders.receive_output(order, JUNE_1, D("11"))
    with pytest.raises(ProductionError, match="greater than zero"):
        orders.receive_output(order, JUNE_1, D("0"))


def test_confirmation_rules(pp):
    order = frame_order(pp, "10")
    with pytest.raises(ProductionError, match="no operation 20"):
        orders.confirm_operation(order, 20, JUNE_1, yield_quantity=D("1"))
    with pytest.raises(ProductionError, match="not recorded at work centre"):
        orders.confirm_operation(order, 10, JUNE_1, activities={"ENGY": D("1")})
    with pytest.raises(ProductionError, match="cannot be negative"):
        orders.confirm_operation(order, 10, JUNE_1, yield_quantity=D("-1"))
    with pytest.raises(ProductionError, match="cannot be negative"):
        orders.confirm_operation(order, 10, JUNE_1, activities={"LAB": D("-1")})
    with pytest.raises(ProductionError, match="Nothing to confirm"):
        orders.confirm_operation(order, 10, JUNE_1)
    orders.confirm_operation(order, 10, JUNE_1, yield_quantity=D("10"), final=True)
    with pytest.raises(ProductionError, match="already finally confirmed"):
        orders.confirm_operation(order, 10, JUNE_1, yield_quantity=D("1"))


def test_partial_confirmations_follow_the_plan(pp):
    order = frame_order(pp)
    first = orders.confirm_operation(order, 10, JUNE_1, yield_quantity=D("40"), scrap_quantity=D("10"))
    # Half the quantity reported: half the planned activity (setup spread over the order).
    half = {"LAB": D("5.000"), "MACH": D("5.250")}
    assert {a.activity_type.code: a.quantity for a in first.activities.all()} == half
    final = orders.confirm_operation(order, 10, JUNE_2, yield_quantity=D("50"), final=True)
    assert {a.activity_type.code: a.quantity for a in final.activities.all()} == half
    assert (first.confirmation_number, final.confirmation_number) == ("0001", "0002")


def test_cancel_only_without_postings(pp):
    clean = frame_order(pp, "5")
    assert orders.cancel_order(clean).status == ProductionOrder.Status.CANCELLED
    with pytest.raises(ProductionError, match="cancelled"):
        orders.cancel_order(clean)
    busy = frame_order(pp, "5")
    orders.issue_components(busy, JUNE_1)
    with pytest.raises(ProductionError, match="has postings"):
        orders.cancel_order(busy)


def test_cancel_and_rule_errors_through_the_api(pp, planner):
    order = frame_order(pp, "5")
    assert planner.post(f"{API}/{order.pk}/cancel/").data["status"] == "CANCELLED"
    again = planner.post(f"{API}/{order.pk}/cancel/")
    assert again.status_code == 400
    assert planner.post(f"{API}/{order.pk}/release/", {"posting_date": "2026-06-01"}, format="json").status_code == 400
    bad = planner.post(f"{API}/", {"material": "RM-2001", "plant": "1000", "quantity": "1",
                                   "start_date": "2026-06-01"}, format="json")
    assert bad.status_code == 400 and "No active BOM" in str(bad.data)
    wrong_location = planner.post(f"{API}/", {"material": "SF-3001", "plant": "1000", "quantity": "1",
                                              "start_date": "2026-06-01", "receipt_storage_location": "9999"},
                                  format="json")
    assert wrong_location.status_code == 400
    released = frame_order(pp, "5")
    bad_issue = planner.post(f"{API}/{released.pk}/issue/", {"posting_date": "2026-06-01", "items": [
        {"material": "RM-2001", "quantity": "1", "storage_location": "9999"}]}, format="json")
    assert bad_issue.status_code == 400
    over = planner.post(f"{API}/{released.pk}/receive/", {"posting_date": "2026-06-01", "quantity": "6"},
                        format="json")
    assert over.status_code == 400 and "over-delivery" in str(over.data)
    bad_confirm = planner.post(f"{API}/{released.pk}/confirmations/", {
        "posting_date": "2026-06-01", "operation_number": 99, "yield_quantity": "1"}, format="json")
    assert bad_confirm.status_code == 400


def test_reversing_an_issue_takes_the_cost_off_the_order(pp):
    order = frame_order(pp, "10")
    document = orders.issue_components(order, JUNE_1)
    assert orders.order_balance(order) == D("950.00")
    reversal = inventory.reverse_material_document(document)
    assert reversal.order_number == order.order_number
    assert orders.order_balance(order) == D("0.00")
    assert orders.issued_quantities(order) == {pp.material("RM-2001").pk: D("0.000")}


# --- roles -----------------------------------------------------------------------------------------

def test_roles_on_orders(pp, client_for):
    order = frame_order(pp, "5")
    url = f"{API}/{order.pk}"
    sales, viewer = client_for(Role.SALES_REP), client_for(Role.VIEWER)
    warehouse, finance_manager = client_for(Role.INVENTORY_MANAGER), client_for(Role.FINANCE_MANAGER)
    assert sales.get(f"{API}/").status_code == 403
    assert viewer.get(f"{url}/").status_code == 200 and viewer.get(f"{url}/costs/").status_code == 200
    assert viewer.post(f"{url}/issue/", {"posting_date": "2026-06-01"}, format="json").status_code == 403
    assert warehouse.post(f"{API}/", {}, format="json").status_code == 403  # the warehouse cannot plan
    assert warehouse.post(f"{url}/confirmations/", {}, format="json").status_code == 403
    assert finance_manager.post(f"{url}/release/", {"posting_date": "2026-06-01"}, format="json").status_code == 403


# --- finance and stock rules for the order cost object ---------------------------------------------

def test_journal_rules_for_orders(pp):
    company, allocation = pp.company, pp.account("6950")
    bank = pp.account("1000")
    cost_center = allocation.journal_lines.model._meta.get_field("cost_center").related_model.objects.get(code="4000")

    def post(lines):
        return finance.post_journal_entry(company_code=company, posting_date=JUNE_1, lines=lines)

    with pytest.raises(finance.PostingError, match="not both"):
        post([finance.LineInput(allocation, "D", D("1"), cost_center=cost_center, order_number="MO1"),
              finance.LineInput(bank, "C", D("1"))])
    with pytest.raises(finance.PostingError, match="cannot take an order"):
        post([finance.LineInput(bank, "D", D("1"), order_number="MO1"),
              finance.LineInput(allocation, "C", D("1"), cost_center=cost_center)])
    with pytest.raises(finance.PostingError, match="cost centre or a production order"):
        post([finance.LineInput(allocation, "D", D("1")), finance.LineInput(bank, "C", D("1"))])
    entry = post([finance.LineInput(allocation, "D", D("1"), order_number="MO1"),
                  finance.LineInput(bank, "C", D("1"))])
    assert entry.lines.get(order_number="MO1").gl_account.account_type == GLAccount.AccountType.EXPENSE


def test_stock_rules_for_orders(pp):
    location = pp.plant.storage_locations.get(code="0001")
    item = [inventory.ItemInput(pp.material("RM-2001"), location, D("1"))]
    with pytest.raises(inventory.StockError, match="needs the order number"):
        inventory.post_goods_movement(movement_type=MovementType.GOODS_ISSUE_ORDER, plant=pp.plant,
                                      posting_date=JUNE_1, items=item)
    with pytest.raises(inventory.StockError, match="cannot refer to a production order"):
        inventory.post_goods_movement(movement_type=MovementType.SCRAPPING, plant=pp.plant, posting_date=JUNE_1,
                                      items=item, order_number="MO1")
    assert not MaterialDocument.objects.filter(order_number="MO1").exists()


# --- graph -----------------------------------------------------------------------------------------

def test_orders_in_the_knowledge_graph(pp, settings, make_user):
    settings.GRAPH_ENABLED = True
    order = frame_order(pp)
    store = MemoryStore()
    processor.rebuild(store=store)
    key = f"1000/{order.order_number}"
    node = store.props("ProductionOrder", key)
    assert (node["status"], node["quantity"], node["planned_cost"]) == ("RELEASED", 100.0, 16425.0)
    assert store.related("ProductionOrder", key, "PRODUCES") == [("Material", "SF-3001", {})]
    assert store.related("ProductionOrder", key, "MADE_AT") == [("Plant", "1000", {})]
    assert store.related("ProductionOrder", key, "CONSUMES") == [("Material", "RM-2001",
                                                                  {"item_number": 10, "quantity": 250.0})]
    assert store.related("ProductionOrder", key, "HAS_OPERATION") == [
        ("Cell", "1000/PRESS3", {"operation_number": 10, "description": "Press frame"})]
    assert "ProductionOrder" in access.allowed_labels(make_user(Role.PRODUCTION_PLANNER, username="g-pp"))
    assert "ProductionOrder" not in access.allowed_labels(make_user(Role.SALES_REP, username="g-sr"))


# --- exact decimals in every response (ADR 0008) ---------------------------------------------------

def test_reports_render_exact_decimal_strings(pp, client_for):
    finance_manager = client_for(Role.FINANCE_MANAGER)
    response = finance_manager.get("/api/production/cost-estimate/?material=FG-4001&plant=1000&quantity=100"
                                   "&date=2026-06-01")
    body = json.loads(response.content)
    assert (body["total"], body["unit_cost"], body["quantity"]) == ("22525.00", "225.25", "100")
    assert body["materials"][1]["price"] == "1.5000"  # moving average price keeps its four decimals
    balance = json.loads(finance_manager.get(
        "/api/finance/reports/trial-balance/?company_code=1000&fiscal_year=2027").content)
    amounts = list(_decimals(balance))
    assert amounts and all(isinstance(value, str) for value in amounts)
    assert balance["total_debit"] == balance["total_credit"] and balance["total_debit"].endswith(".00")


def _decimals(data):
    """Every value under a money-like key in a nested response."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("debit", "credit", "balance", "total_debit", "total_credit") and value is not None:
                yield value
            else:
                yield from _decimals(value)
    elif isinstance(data, list):
        for value in data:
            yield from _decimals(value)
