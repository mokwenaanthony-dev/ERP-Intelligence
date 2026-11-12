"""Knowledge graph against a real Neo4j: projection through the outbox, then the Cypher queries and API.

Runs in CI (a Neo4j service container) and locally when NEO4J_URI points at a Neo4j, e.g. the
Docker one: ``NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=... pytest tests/test_graph_neo4j.py``.
Skipped otherwise. Expected figures come from the relational database, so the graph must agree
with the ERP.
"""
import itertools
from decimal import Decimal
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db.models import Sum

from apps.accounts.models import Role
from apps.graph import access, processor, queries
from apps.graph.store import get_store
from apps.masterdata.models import BusinessPartner
from apps.sales.models import Invoice, SalesOrder

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not settings.NEO4J_URI, reason="No Neo4j configured (set NEO4J_URI)."),
]


@pytest.fixture
def graph(demo):
    """Two months of trading, projected into an empty Neo4j through the outbox."""
    store = get_store()
    store.clear()
    store.ensure_schema(processor.projection.ALL_LABELS)
    call_command("seed_demo_activity", "--months", "2", stdout=StringIO())
    # Everything since the demo company was created is in the outbox: project it the normal way.
    result = processor.process_outbox(limit=100000, store=store)
    assert result["failed"] == 0 and result["processed"] > 100
    return demo


_names = itertools.count()


def labels_for(make_user, role):
    return access.allowed_labels(make_user(role, username=f"g-{role}-{next(_names)}"))


def coastal():
    return BusinessPartner.objects.get(name="Coastal Hardware (Pty) Ltd")


def test_customer_360_agrees_with_the_erp(graph, make_user):
    customer = coastal()
    result = queries.customer_360(customer.number, labels_for(make_user, Role.FINANCE_MANAGER))
    assert result["business_partner"]["name"] == "Coastal Hardware (Pty) Ltd"
    assert {r["role"] for r in result["roles"]} == {"CUSTOMER_OF"}

    orders = SalesOrder.objects.filter(customer=customer)
    assert sum(row["orders"] for row in result["sales_orders"]) == orders.count()
    invoices = Invoice.objects.filter(customer=customer, status="POSTED")
    billed = invoices.aggregate(total=Sum("gross_amount"))["total"]
    assert result["billing"]["invoices"] == invoices.count()
    assert result["billing"]["billed"] == pytest.approx(float(billed), abs=0.01)
    # Coastal pays about a month late: after two months nothing has been paid yet.
    assert result["billing"]["paid"] == 0
    assert result["billing"]["open_amount"] == pytest.approx(float(billed), abs=0.01)
    assert result["top_materials"] and result["recent_documents"]
    assert "as_supplier" in result


def test_customer_360_respects_roles(graph, make_user):
    customer = coastal().number
    sales_view = queries.customer_360(customer, labels_for(make_user, Role.SALES_REP))
    assert "as_supplier" not in sales_view
    assert {doc["type"] for doc in sales_view["recent_documents"]} <= {"SalesOrder", "Delivery", "Invoice", "Payment"}
    assert queries.customer_360("9999999", labels_for(make_user, Role.SALES_REP)) is None


def test_document_flow_from_an_order(graph, make_user):
    order = SalesOrder.objects.filter(customer=graph.customer).order_by("order_number").first()
    flow = queries.document_flow("1000", order.order_number, labels_for(make_user, Role.ACCOUNTANT))
    types = [doc["type"] for doc in flow["documents"]]
    assert types[:3] == ["SalesOrder", "Delivery", "Invoice"]
    assert "Payment" in types  # Acme paid its March invoices in April
    sales_order = flow["documents"][0]
    assert sales_order["links"] == [{"type": "DELIVERED_BY", "to": f"1000/{order.deliveries.get().delivery_number}"}]


def test_document_flow_from_a_payment_goes_upstream(graph, make_user):
    payment = next(doc for doc in queries.document_flow(
        "1000", SalesOrder.objects.filter(customer=graph.customer).order_by("order_number").first().order_number,
        labels_for(make_user, Role.ACCOUNTANT))["documents"] if doc["type"] == "Payment")
    flow = queries.document_flow("1000", payment["number"], labels_for(make_user, Role.ACCOUNTANT))
    types = {doc["type"] for doc in flow["documents"]}
    assert {"Payment", "Invoice", "Delivery", "SalesOrder"} <= types


def test_document_flow_rules(graph, make_user):
    accountant = labels_for(make_user, Role.ACCOUNTANT)
    assert queries.document_flow("1000", "SO99999999", accountant) is None
    with pytest.raises(ValueError):
        queries.document_flow("1000", "XX00000001", accountant)
    with pytest.raises(PermissionError):
        queries.document_flow("1000", "PO00000001", labels_for(make_user, Role.SALES_REP))
    purchase = queries.document_flow("1000", "PO00000001", accountant)
    assert [doc["type"] for doc in purchase["documents"]][:3] == ["PurchaseOrder", "GoodsReceipt", "SupplierInvoice"]


def test_supplier_dependency(graph, make_user):
    rows = queries.supplier_dependency("1000", labels_for(make_user, Role.FINANCE_MANAGER))
    by_material = {row["material"]: row for row in rows}
    assert by_material["TG-1003"]["suppliers"] == ["Gauteng Components (Pty) Ltd"]
    assert by_material["TG-1003"]["single_source"] is True
    assert by_material["TG-1003"]["sales_value"] > 0 and by_material["TG-1003"]["customers"]
    purchasing_view = queries.supplier_dependency("1000", labels_for(make_user, Role.PURCHASING_OFFICER))
    assert "customers" not in purchasing_view[0]  # purchasing does not see sales orders


def test_graphrag_context_is_grounded_and_permission_filtered(graph, make_user):
    order = SalesOrder.objects.filter(customer=graph.customer).order_by("order_number").first()
    key = f"1000/{order.order_number}"
    context = queries.context("SalesOrder", key, labels_for(make_user, Role.SALES_REP), depth=2)
    assert context["nodes"][key]["type"] == "SalesOrder"
    assert any(fact["predicate"] == "CONTAINS" for fact in context["facts"])
    assert all(node["type"] != "Employee" for node in context["nodes"].values())
    assert context["facts"][0]["text"].count(" ") >= 2

    hr = labels_for(make_user, Role.HR_ADMIN)
    people = queries.context("Employee", "1000/PN00000002", hr, depth=3)
    assert {node["type"] for node in people["nodes"].values()} >= {"Employee", "Position", "OrgUnit", "CostCenter"}
    assert all("monthly_salary" not in node for node in people["nodes"].values())
    with pytest.raises(PermissionError):
        queries.context("Employee", "1000/PN00000002", labels_for(make_user, Role.SALES_REP))
    assert queries.context("SalesOrder", "1000/NOPE", labels_for(make_user, Role.SALES_REP)) is None


def test_graph_api(graph, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    customer = coastal().number
    view = accountant.get(f"/api/graph/customer-360/{customer}/")
    assert view.status_code == 200, view.data
    assert accountant.get("/api/graph/customer-360/9999999/").status_code == 404
    assert client_for(Role.HR_ADMIN).get(f"/api/graph/customer-360/{customer}/").status_code == 403

    flow = accountant.get("/api/graph/document-flow/", {"company_code": "1000", "document": "SO00000001"})
    assert flow.status_code == 200 and flow.data["documents"]
    assert accountant.get("/api/graph/document-flow/", {"company_code": "1000"}).status_code == 400
    assert accountant.get("/api/graph/document-flow/", {"company_code": "1000", "document": "XX1"}).status_code == 400
    flow_url = "/api/graph/document-flow/"
    assert accountant.get(flow_url, {"company_code": "1000", "document": "SO99999999"}).status_code == 404
    sales = client_for(Role.SALES_REP)
    assert sales.get(flow_url, {"company_code": "1000", "document": "PO00000001"}).status_code == 403

    dependency = accountant.get("/api/graph/supplier-dependency/", {"company_code": "1000"})
    assert dependency.status_code == 200 and dependency.data["materials"]
    assert sales.get("/api/graph/supplier-dependency/", {"company_code": "1000"}).status_code == 403

    context = sales.get("/api/graph/context/", {"label": "SalesOrder", "key": "1000/SO00000001", "depth": 1})
    assert context.status_code == 200 and context.data["facts"]
    assert sales.get("/api/graph/context/", {"label": "Employee", "key": "1000/PN00000001"}).status_code == 403
    assert sales.get("/api/graph/context/", {"label": "Nope", "key": "x"}).status_code == 400
    assert sales.get("/api/graph/context/", {"label": "SalesOrder"}).status_code == 400
    assert sales.get("/api/graph/context/", {"label": "SalesOrder", "key": "x", "depth": "a"}).status_code == 400
    assert sales.get("/api/graph/context/", {"label": "SalesOrder", "key": "1000/NOPE"}).status_code == 404

    status = accountant.get("/api/graph/status/").data
    assert status["enabled"] is True and status["pending_events"] == 0
    rebuilt = client_for(Role.ADMIN).post("/api/graph/rebuild/")
    assert rebuilt.status_code == 200 and rebuilt.data["projected"] > 100


def test_money_is_stored_as_numbers(graph):
    [row] = get_store().read("MATCH (i:Invoice) RETURN i.gross_amount AS amount LIMIT 1")
    assert isinstance(row["amount"], float)
    assert Decimal(str(row["amount"])) > 0


def test_where_used_across_bom_levels_and_open_orders(graph, make_user, client_for):
    """Steel sheet goes into the pressed frame, which goes into the bracket: a shortage of steel puts
    every open bracket order at risk. The answer must come from two BOM levels in the graph."""
    from datetime import date

    from apps.masterdata.models import Material
    from apps.organization.models import SalesOrganization
    from apps.sales import services as sales

    call_command("setup_demo_production", stdout=StringIO())
    order = sales.create_sales_order(
        sales_organization=SalesOrganization.objects.get(code="1000"), customer=coastal(),
        order_date=date(2026, 6, 2),
        items=[sales.OrderItemInput(Material.objects.get(number="FG-4001"), Decimal("20"))])
    sales.confirm_order(order)
    assert processor.process_outbox(limit=100000, store=get_store())["failed"] == 0

    result = queries.where_used("RM-2001", labels_for(make_user, Role.SALES_REP))
    assert [(row["level"], row["component"], row["product"]) for row in result["used_in"]] == [
        (1, "RM-2001", "SF-3001"), (2, "SF-3001", "FG-4001")]
    assert result["used_in"][0]["quantity"] == 2.5
    [open_order] = result["open_sales_orders"]
    assert (open_order["sales_order"], open_order["product"], open_order["customer"]) == (
        f"1000/{order.order_number}", "FG-4001", "Coastal Hardware (Pty) Ltd")

    # Roles that may not see sales get the product structure only.
    hidden = queries.where_used("RM-2001", labels_for(make_user, Role.HR_ADMIN))
    assert "open_sales_orders" not in hidden and len(hidden["used_in"]) == 2
    assert queries.where_used("NOPE", labels_for(make_user, Role.SALES_REP)) is None

    api = client_for(Role.PRODUCTION_PLANNER)
    response = api.get("/api/graph/where-used/rm-2001/")
    assert response.status_code == 200 and response.data["open_sales_orders"]
    assert api.get("/api/graph/where-used/NOPE/").status_code == 404

    cell = get_store().read("MATCH (c:Cell {key: '1000/PRESS3'})-[:PART_OF*3]->(p:Plant) "
                            "RETURN c.uns_path AS path, p.key AS plant")
    assert cell == [{"path": "1000/1000/assy/l1/press3", "plant": "1000"}]


def test_where_used_shows_open_production_orders(graph, make_user):
    """A planned frame order needs steel sheet: where-used warns the planner before the shortage bites."""
    from datetime import date

    from apps.masterdata.models import Material
    from apps.organization.models import Plant
    from apps.production import orders

    call_command("setup_demo_production", stdout=StringIO())
    order = orders.create_order(plant=Plant.objects.get(code="1000"), material=Material.objects.get(number="SF-3001"),
                                quantity=Decimal("40"), start_date=date(2026, 6, 1))
    orders.release_order(order, date(2026, 6, 1))
    assert processor.process_outbox(limit=100000, store=get_store())["failed"] == 0

    result = queries.where_used("RM-2001", labels_for(make_user, Role.PRODUCTION_PLANNER))
    [planned] = result["open_production_orders"]
    assert (planned["production_order"], planned["status"], planned["product"], planned["quantity"]) == (
        f"1000/{order.order_number}", "RELEASED", "SF-3001", 100.0)  # 40 frames x 2.5 kg
    assert "open_production_orders" not in queries.where_used("RM-2001", labels_for(make_user, Role.SALES_REP))


def test_shop_floor_shows_down_cells_and_the_orders_they_hold_up(graph, make_user, client_for):
    """Wave 6d: a cell reports DOWN; the graph answers "which open orders does that hold up?"."""
    import json
    from datetime import date

    from apps.masterdata.models import Material
    from apps.organization.models import Plant
    from apps.production import orders
    from apps.uns import listener

    call_command("setup_demo_production", stdout=StringIO())
    order = orders.create_order(plant=Plant.objects.get(code="1000"), material=Material.objects.get(number="SF-3001"),
                                quantity=Decimal("40"), start_date=date(2026, 6, 1))
    orders.release_order(order, date(2026, 6, 1))
    down = json.dumps({"schema": "equipment-state/v1", "event_id": None, "timestamp": "2026-06-01T09:00:00.000+00:00",
                       "source": "mes-edge/press3",
                       "data": {"state": "DOWN", "order": None, "operation_number": None,
                                "since": "2026-06-01T09:00:00.000+00:00"}}).encode()
    assert listener.handle_state("1000/1000/assy/l1/press3/state/status", down) == "created"
    assert processor.process_outbox(limit=100000, store=get_store())["failed"] == 0

    planner = queries.shop_floor("1000", labels_for(make_user, Role.PRODUCTION_PLANNER))
    assert planner["states"] == {"DOWN": 1, "UNKNOWN": 1}  # ASSY1 has not reported: unknown, not guessed
    [press] = planner["needs_attention"]
    assert (press["cell"], press["state"], press["since"][:19]) == ("1000/PRESS3", "DOWN", "2026-06-01T09:00:00")
    [held] = press["held_up"]
    assert (held["production_order"], held["operation_number"], held["product"]) == (
        f"1000/{order.order_number}", 10, "SF-3001")
    assert held["open_sales_orders"] == []  # nobody has ordered frames on their own

    sales = queries.shop_floor("1000", labels_for(make_user, Role.SALES_REP))
    assert sales["needs_attention"][0]["held_up"] == []  # a sales rep may not see production orders

    api = client_for(Role.PRODUCTION_PLANNER)  # one user: client_for names each user after its role
    response = api.get("/api/graph/shop-floor/", {"plant": "1000"})
    assert response.status_code == 200 and response.data["needs_attention"][0]["cell"] == "1000/PRESS3"
    assert api.get("/api/graph/shop-floor/").status_code == 400
