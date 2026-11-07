"""Knowledge graph without a Neo4j server: the outbox, the projection rules and the access rules.

The projection writes to ``MemoryStore``, which applies the same operations as Neo4jStore in
Python, so these tests run everywhere. ``test_graph_neo4j.py`` runs the Cypher against a real Neo4j.
"""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import Role
from apps.finance import services as finance
from apps.graph import access, processor, projection
from apps.graph.models import OutboxEvent
from apps.graph.store import MemoryStore, Neo4jStore, _name, clean
from apps.masterdata.models import CustomerRole, Material, UnitOfMeasure
from apps.organization.models import PurchasingOrganization, SalesOrganization, StorageLocation
from apps.purchasing import services as purchasing
from apps.sales import services as sales

pytestmark = pytest.mark.django_db
D = Decimal


@pytest.fixture
def graph_off(settings):
    settings.GRAPH_ENABLED = False  # CI sets NEO4J_URI, which switches the graph on for every test


@pytest.fixture
def graph_on(settings):
    settings.GRAPH_ENABLED = True
    OutboxEvent.objects.all().delete()  # start from the changes this test makes
    return MemoryStore()


def project_all(store):
    result = processor.process_outbox(limit=100000, store=store)
    assert result["failed"] == 0, OutboxEvent.objects.exclude(last_error="").values_list("last_error", flat=True)
    return result


def sell(demo, quantity="10"):
    widget = Material.objects.get(number="TG-1001")
    order = sales.create_sales_order(
        sales_organization=SalesOrganization.objects.get(code="1000"), customer=demo.customer,
        order_date=date(2026, 5, 10), items=[sales.OrderItemInput(widget, D(quantity))])
    sales.confirm_order(order)
    delivery = sales.post_delivery(order, delivery_date=date(2026, 5, 12),
                                   storage_location=StorageLocation.objects.get(code="0001"))
    invoice = sales.post_invoice(delivery, invoice_date=date(2026, 5, 15))
    return order, delivery, invoice


def buy(demo):
    order = purchasing.create_purchase_order(
        purchasing_organization=PurchasingOrganization.objects.get(code="1000"), supplier=demo.supplier,
        order_date=date(2026, 5, 4),
        items=[purchasing.OrderItemInput(Material.objects.get(number="TG-1002"), D("5"), D("300.00"))])
    purchasing.release_order(order)
    receipt = purchasing.post_goods_receipt(order, receipt_date=date(2026, 5, 6))
    invoice = purchasing.post_supplier_invoice(order, invoice_date=date(2026, 5, 8), supplier_reference="HS-77")
    return order, receipt, invoice


# --- outbox --------------------------------------------------------------------------------------

def test_no_events_while_the_graph_is_off(graph_off, demo):
    assert OutboxEvent.objects.count() == 0
    assert processor.process_outbox() == {"enabled": False, "processed": 0, "failed": 0}


def test_changes_write_events_in_the_same_transaction(demo, graph_on):
    material = Material.objects.create(number="KG-1", description="Kit", base_unit=UnitOfMeasure.objects.get(code="EA"))
    event = OutboxEvent.objects.get()
    assert (event.aggregate_type, event.aggregate_id) == ("masterdata.material", material.pk)
    assert str(event) == f"#{event.pk} masterdata.material:{material.pk} (pending)"
    material.description = "Starter kit"
    material.save()
    assert project_all(graph_on) == {"enabled": True, "processed": 2, "failed": 0}
    assert graph_on.props("Material", "KG-1")["description"] == "Starter kit"
    assert processor.process_outbox(store=graph_on)["processed"] == 0  # nothing left


def test_deleting_a_record_removes_its_node(demo, graph_on):
    unit = UnitOfMeasure.objects.get(code="EA")
    material = Material.objects.create(number="KG-2", description="Temp", base_unit=unit)
    project_all(graph_on)
    material.delete()
    project_all(graph_on)
    assert ("Material", "KG-2") not in graph_on.nodes


def test_removing_a_role_refreshes_the_partner(demo, graph_on):
    role = CustomerRole.objects.get(business_partner=demo.customer)
    role.delete()
    events = list(OutboxEvent.objects.values_list("aggregate_type", flat=True))
    assert events == ["masterdata.businesspartner"]


# --- projection ----------------------------------------------------------------------------------

def test_master_data_and_organisation(demo, graph_on):
    processor.rebuild(store=graph_on)
    customer = demo.customer.number
    assert graph_on.labels("BusinessPartner", customer) == {"BusinessPartner", "Customer"}
    assert graph_on.related("BusinessPartner", customer, "CUSTOMER_OF")[0][:2] == ("CompanyCode", "1000")
    assert graph_on.labels("BusinessPartner", demo.supplier.number) == {"BusinessPartner", "Supplier"}
    assert graph_on.related("Plant", "1000", "ASSIGNED_TO") == [("ProfitCenter", "1000/PC1000", {})]
    assert graph_on.related("CostCenter", "1000/2000", "BELONGS_TO") == [("ProfitCenter", "1000/PC1000", {})]
    assert graph_on.related("OrgUnit", "1000/SALES", "CHARGED_TO") == [("CostCenter", "1000/2000", {})]
    assert graph_on.related("Position", "1000/SALES/SREP", "IN") == [("OrgUnit", "1000/SALES", {})]
    employee = graph_on.props("Employee", "1000/PN00000002")
    assert employee["name"] == "Sipho Dlamini"
    assert "monthly_salary" not in employee and "tax_reference" not in employee  # pay data stays out
    assert graph_on.related("Employee", "1000/PN00000002", "HOLDS") == [("Position", "1000/SALES/SREP", {})]


def test_order_to_cash_chain(demo, graph_on):
    order, delivery, invoice = sell(demo)
    finance.post_incoming_payment(company_code=demo.company, customer=demo.customer, payment_date=date(2026, 6, 10),
                                  amount=invoice.gross_amount, bank_account=demo.account("1000"),
                                  allocations=[(invoice.journal_entry.lines.get(line_number=1).pk,
                                                invoice.gross_amount)])
    project_all(graph_on)
    so, dl, inv = "1000/SO00000001", "1000/DL00000001", "1000/IN00000001"
    assert graph_on.related("BusinessPartner", demo.customer.number, "PLACED") == [("SalesOrder", so, {})]
    assert graph_on.related("SalesOrder", so, "CONTAINS") == [
        ("Material", "TG-1001", {"item_number": 10, "quantity": 10.0, "net_price": 199.0})]
    assert graph_on.props("SalesOrder", so)["status"] == "COMPLETED"
    assert graph_on.related("SalesOrder", so, "DELIVERED_BY") == [("Delivery", dl, {})]
    assert graph_on.related("Delivery", dl, "SHIPPED_FROM") == [("Plant", "1000", {})]
    assert graph_on.related("Delivery", dl, "BILLED_BY") == [("Invoice", inv, {})]
    assert graph_on.props("Invoice", inv)["gross_amount"] == 2288.5
    [(_, payment, props)] = graph_on.related("Invoice", inv, "PAID_BY")
    assert (payment, props) == ("1000/2027/DZ00000001", {"amount": 2288.5})
    assert graph_on.props("Payment", payment)["direction"] == "INCOMING"
    assert graph_on.related("BusinessPartner", demo.customer.number, "PAID") == [("Payment", payment, {})]


def test_reversals_and_cancellations_update_status(demo, graph_on):
    _, delivery, invoice = sell(demo)
    sales.cancel_invoice(invoice)
    sales.reverse_delivery(delivery)
    project_all(graph_on)
    assert graph_on.props("Invoice", "1000/IN00000001")["status"] == "CANCELLED"
    assert graph_on.props("Delivery", "1000/DL00000001")["status"] == "REVERSED"


def test_payment_reversal_marks_the_payment_reversed(demo, graph_on):
    payment = finance.post_incoming_payment(company_code=demo.company, customer=demo.customer,
                                            payment_date=date(2026, 6, 10), amount=D("100.00"),
                                            bank_account=demo.account("1000"))
    finance.reverse_journal_entry(payment)
    project_all(graph_on)
    assert graph_on.props("Payment", "1000/2027/DZ00000001")["status"] == "REVERSED"
    assert ("Payment", "1000/2027/DZ00000002") not in graph_on.nodes  # the reversal is not a payment of its own


def test_procure_to_pay_chain(demo, graph_on):
    order, receipt, invoice = buy(demo)
    finance.post_outgoing_payment(company_code=demo.company, supplier=demo.supplier, payment_date=date(2026, 6, 1),
                                  amount=invoice.gross_amount, bank_account=demo.account("1000"))
    project_all(graph_on)
    po, supplier = "1000/PO00000001", demo.supplier.number
    assert graph_on.related("PurchaseOrder", po, "ORDERED_FROM") == [("BusinessPartner", supplier, {})]
    assert graph_on.related("BusinessPartner", supplier, "SUPPLIES") == [("Material", "TG-1002", {})]
    assert graph_on.related("PurchaseOrder", po, "RECEIVED_BY") == [("GoodsReceipt", "1000/GR00000001", {})]
    assert graph_on.related("PurchaseOrder", po, "BILLED_BY") == [("SupplierInvoice", "1000/IV00000001", {})]
    [(_, payment, _)] = graph_on.related("SupplierInvoice", "1000/IV00000001", "PAID_BY")
    assert graph_on.related("Payment", payment, "PAID_TO") == [("BusinessPartner", supplier, {})]
    assert graph_on.props("Payment", payment)["direction"] == "OUTGOING"


def test_cancelled_purchase_documents(demo, graph_on):
    order, receipt, invoice = buy(demo)
    purchasing.cancel_supplier_invoice(invoice)
    purchasing.reverse_goods_receipt(receipt)
    project_all(graph_on)
    assert graph_on.props("SupplierInvoice", "1000/IV00000001")["status"] == "CANCELLED"
    assert graph_on.props("GoodsReceipt", "1000/GR00000001")["status"] == "REVERSED"


def test_rebuild_matches_incremental_projection(demo, graph_on):
    sell(demo)
    buy(demo)
    incremental = MemoryStore()
    project_all(incremental)
    rebuilt = MemoryStore()
    assert processor.rebuild(store=rebuilt) > 20
    assert set(rebuilt.edges) >= set(incremental.edges)  # rebuild also covers master data saved before the switch-on
    assert OutboxEvent.objects.filter(processed_at__isnull=True).count() == 0
    processor.rebuild(store=rebuilt, clear=True)
    assert rebuilt.nodes  # cleared and rebuilt


# --- processing failures -------------------------------------------------------------------------

def test_a_bad_record_does_not_block_the_rest(demo, graph_on, monkeypatch):
    unit = UnitOfMeasure.objects.get(code="EA")
    Material.objects.create(number="OK-1", description="Fine", base_unit=unit)
    bad = Material.objects.create(number="BAD-1", description="Breaks", base_unit=unit)
    original = projection.project

    def flaky(store, aggregate_type, aggregate_id, payload=None):
        if aggregate_id == bad.pk and aggregate_type == "masterdata.material":
            raise RuntimeError("boom")
        return original(store, aggregate_type, aggregate_id, payload)

    monkeypatch.setattr(projection, "project", flaky)
    assert processor.process_outbox(store=graph_on) == {"enabled": True, "processed": 1, "failed": 1}
    event = OutboxEvent.objects.get(aggregate_id=bad.pk)
    assert (event.attempts, event.failed, event.last_error) == (1, False, "RuntimeError: boom")
    for _ in range(processor.MAX_ATTEMPTS - 1):
        processor.process_outbox(store=graph_on)
    event.refresh_from_db()
    assert event.failed is True
    assert processor.status()["failed_events"] == 1


def test_neo4j_down_leaves_events_pending(demo, graph_on, monkeypatch):
    Material.objects.create(number="KG-3", description="X", base_unit=UnitOfMeasure.objects.get(code="EA"))

    def down():
        raise ConnectionError("no route to Neo4j")

    monkeypatch.setattr(graph_on, "flush", down)
    result = processor.process_outbox(store=graph_on)
    assert (result["processed"], result["error"]) == (0, "no route to Neo4j")
    assert OutboxEvent.objects.get().last_error == "flush: no route to Neo4j"
    assert processor.status()["pending_events"] == 1


def test_records_deleted_before_processing_are_skipped(demo, graph_on):
    OutboxEvent.objects.create(aggregate_type="masterdata.material", aggregate_id=999999)
    assert project_all(graph_on)["processed"] == 1


# --- store ---------------------------------------------------------------------------------------

def test_names_are_checked_and_values_cleaned():
    assert _name("SalesOrder") == "SalesOrder"
    for bad in ("Sales Order", "x`) DETACH DELETE n //", "1abc", ""):
        with pytest.raises(ValueError):
            _name(bad)
    assert clean({"amount": D("1.50"), "day": date(2026, 1, 1), "x": None, "obj": object}) == {
        "amount": 1.5, "day": date(2026, 1, 1), "x": None, "obj": str(object)}


def test_neo4j_store_builds_parameterised_cypher():
    store = object.__new__(Neo4jStore)
    store.pending = []
    store.node("Material", "TG-1", {"description": "Widget"}, extra_labels=["Product"])
    store.edge("SalesOrder", "1000/SO1", "CONTAINS", "Material", "TG-1", {"quantity": D("2")},
               merge_on={"item_number": 10})
    store.edge("Plant", "1000", "BELONGS_TO", "CompanyCode", "1000")
    store.clear_edges("SalesOrder", "1000/SO1", "CONTAINS")
    store.delete_node("Material", "TG-1")
    cyphers = [cypher for cypher, _ in store.pending]
    assert cyphers[0] == "MERGE (n:`Material` {key: $key}) SET n += $props SET n:`Product`"
    assert cyphers[1].endswith("MERGE (a)-[r:`CONTAINS` {item_number: $m_item_number}]->(b) SET r += $props")
    assert "MERGE (a)-[r:`BELONGS_TO`]->(b)" in cyphers[2]
    assert cyphers[3] == "MATCH (:`SalesOrder` {key: $key})-[r:`CONTAINS`]->() DELETE r"
    assert cyphers[4] == "MATCH (n:`Material` {key: $key}) DETACH DELETE n"
    assert store.pending[1][1]["props"] == {"quantity": 2.0}
    with pytest.raises(ValueError):
        store.node("Bad Label", "x")


# --- access and API ------------------------------------------------------------------------------

def test_roles_see_only_their_part_of_the_graph(make_user):
    seen = {role: access.allowed_labels(make_user(role, username=f"u-{role}")) for role in
            (Role.HR_ADMIN, Role.SALES_REP, Role.PURCHASING_OFFICER, Role.FINANCE_MANAGER, Role.ADMIN)}

    def labels(role):
        return seen[role]

    assert "Employee" in labels(Role.HR_ADMIN) and "SalesOrder" not in labels(Role.HR_ADMIN)
    assert "Employee" not in labels(Role.SALES_REP) and "PurchaseOrder" not in labels(Role.SALES_REP)
    assert {"PurchaseOrder", "SupplierInvoice"} <= labels(Role.PURCHASING_OFFICER)
    assert "Employee" not in labels(Role.FINANCE_MANAGER)
    assert labels(Role.ADMIN) == set(projection.ALL_LABELS)


def test_api_when_the_graph_is_off(graph_off, demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    status = accountant.get("/api/graph/status/")
    assert (status.status_code, status.data["enabled"]) == (200, False)
    assert accountant.get(f"/api/graph/customer-360/{demo.customer.number}/").status_code == 503
    assert client_for(Role.ADMIN).post("/api/graph/rebuild/").status_code == 503
    assert accountant.post("/api/graph/rebuild/").status_code == 403


def test_commands(graph_off, demo, settings, monkeypatch):
    with pytest.raises(CommandError, match="NEO4J_URI"):
        call_command("graph_rebuild", stdout=StringIO())
    out = StringIO()
    call_command("graph_process", stdout=out)
    assert "off" in out.getvalue()

    settings.GRAPH_ENABLED = True
    store = MemoryStore()
    monkeypatch.setattr(processor, "get_store", lambda: store)
    out = StringIO()
    call_command("graph_rebuild", "--clear", stdout=out)
    assert "Projected" in out.getvalue() and store.nodes
    Material.objects.create(number="KG-9", description="New", base_unit=UnitOfMeasure.objects.get(code="EA"))
    out = StringIO()
    call_command("graph_process", stdout=out)
    assert "Processed 1 events, 0 failed." in out.getvalue()
    monkeypatch.setattr(store, "flush", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    Material.objects.create(number="KG-10", description="New", base_unit=UnitOfMeasure.objects.get(code="EA"))
    out = StringIO()
    call_command("graph_process", stdout=out)
    assert "Neo4j error: down" in out.getvalue()
