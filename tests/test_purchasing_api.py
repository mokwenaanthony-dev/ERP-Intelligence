"""Purchasing REST API: orders, goods receipts, supplier invoices and the GR/IR report, end to end."""
import pytest

from apps.accounts.models import Role
from apps.purchasing.models import GoodsReceipt, PurchaseOrder

pytestmark = pytest.mark.django_db

ORDERS = "/api/purchasing/orders/"
RECEIPTS = "/api/purchasing/goods-receipts/"
INVOICES = "/api/purchasing/invoices/"


def order_payload(demo, **overrides):
    payload = {
        "purchasing_organization": "1000", "supplier": demo.supplier.number, "order_date": "2026-05-04",
        "delivery_date": "2026-05-11", "supplier_reference": "Q-311",
        "items": [{"material": "TG-1001", "quantity": "50", "net_price": "125.00"},
                  {"material": "TG-1002", "quantity": "20", "net_price": "310.00"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def buyer(client_for):
    return client_for(Role.PURCHASING_OFFICER)


@pytest.fixture
def warehouse(client_for):
    return client_for(Role.INVENTORY_MANAGER)


@pytest.fixture
def accountant(client_for):
    return client_for(Role.ACCOUNTANT)


def released(buyer, demo, **overrides):
    order = buyer.post(ORDERS, order_payload(demo, **overrides), format="json").data
    buyer.post(f"{ORDERS}{order['id']}/release/")
    return order


def test_procure_to_pay_via_api(demo, buyer, warehouse, accountant):
    created = buyer.post(ORDERS, order_payload(demo), format="json")
    assert created.status_code == 201, created.data
    order = created.data
    assert (order["order_number"], order["status"], order["net_total"]) == ("PO00000001", "OPEN", "12450.00")
    assert (order["items"][0]["storage_location"], order["items"][0]["tax_code"]) == ("0001", "I1")
    assert order["created_by"] == buyer.user.pk
    assert buyer.post(f"{ORDERS}{order['id']}/release/").data["status"] == "RELEASED"

    received = warehouse.post(RECEIPTS, {"purchase_order": "PO00000001", "receipt_date": "2026-05-11",
                                         "delivery_note": "DN-55"}, format="json")
    assert received.status_code == 201, received.data
    assert (received.data["receipt_number"], received.data["material_document"],
            received.data["journal_entry"]) == ("GR00000001", "MD00000002", "WE00000002")
    assert received.data["items"][0]["value"] == "6250.00"

    gr_ir = accountant.get("/api/purchasing/reports/gr-ir/", {"company_code": "1000"}).data
    assert (len(gr_ir["items"]), str(gr_ir["open_balance"]), gr_ir["reconciled"]) == (2, "12450.00", True)

    invoiced = accountant.post(INVOICES, {"purchase_order": "PO00000001", "supplier_reference": "HS-9001",
                                          "invoice_date": "2026-05-15"}, format="json")
    assert invoiced.status_code == 201, invoiced.data
    assert (invoiced.data["invoice_number"], invoiced.data["gross_amount"], invoiced.data["open_amount"],
            invoiced.data["journal_entry"]) == ("IV00000001", "14317.50", "14317.50", "KR00000001")
    detail = buyer.get(f"{ORDERS}{order['id']}/").data
    assert (detail["status"], detail["receipts"], detail["invoices"]) == ("COMPLETED", ["GR00000001"], ["IV00000001"])
    assert detail["items"][0]["invoiced_quantity"] == "50.000"

    paid = accountant.post("/api/finance/outgoing-payments/", {
        "company_code": "1000", "supplier": demo.supplier.number, "payment_date": "2026-06-10",
        "amount": "14317.50", "bank_account": "1000",
    }, format="json")
    assert paid.status_code == 201, paid.data
    assert accountant.get(f"{INVOICES}{invoiced.data['id']}/").data["open_amount"] == "0.00"


def test_partial_receipt_and_invoice_with_price_difference(demo, buyer, warehouse, accountant):
    order = released(buyer, demo)
    response = warehouse.post(RECEIPTS, {"purchase_order": order["order_number"], "receipt_date": "2026-05-11",
                                         "items": [{"item_number": 10, "quantity": "30", "storage_location": "0002"}]},
                              format="json")
    assert response.status_code == 201, response.data
    assert response.data["items"] == [{"item_number": 10, "material": "TG-1001", "storage_location": "0002",
                                       "quantity": "30.000", "value": "3750.00"}]
    invoice = accountant.post(INVOICES, {
        "purchase_order": order["order_number"], "supplier_reference": "HS-1", "invoice_date": "2026-05-15",
        "items": [{"item_number": 10, "quantity": "30", "net_price": "128.00"}],
    }, format="json")
    assert invoice.status_code == 201, invoice.data
    assert dict(invoice.data["items"][0]) == {
        "item_number": 10, "material": "TG-1001", "quantity": "30.000", "order_price": "125.00",
        "net_price": "128.00", "net_amount": "3840.00", "gr_ir_amount": "3750.00", "price_difference": "90.00",
        "tax_code": "I1", "tax_amount": "576.00",
    }
    assert buyer.get(ORDERS, {"status": "RELEASED"}).data["count"] == 1


def test_order_input_errors(demo, buyer):
    bad = buyer.post(ORDERS, order_payload(demo, items=[
        {"material": "TG-1001", "quantity": "1", "net_price": "1", "tax_code": "ZZ"},
        {"material": "TG-1001", "quantity": "1", "net_price": "1", "storage_location": "9999"},
    ]), format="json")
    assert bad.status_code == 400
    assert set(bad.data["items"][0]) == {"tax_code"}
    assert set(bad.data["items"][1]) == {"storage_location"}
    assert buyer.post(ORDERS, order_payload(demo, items=[{"material": "TG-1001", "quantity": "0", "net_price": "1"}]),
                      format="json").status_code == 400
    rule = buyer.post(ORDERS, order_payload(demo, supplier=demo.customer.number), format="json")
    assert rule.status_code == 400
    assert "not a supplier" in str(rule.data["non_field_errors"])
    wrong_tax = buyer.post(ORDERS, order_payload(demo, items=[
        {"material": "TG-1001", "quantity": "1", "net_price": "1", "tax_code": "O1"}]), format="json")
    assert "not an input tax code" in str(wrong_tax.data)


def test_changing_items_and_cancelling(demo, buyer):
    order = buyer.post(ORDERS, order_payload(demo), format="json").data
    patched = buyer.patch(f"{ORDERS}{order['id']}/", {"items": [
        {"material": "TG-1002", "quantity": "5", "net_price": "305.00", "tax_code": "I0"},
    ]}, format="json")
    assert patched.status_code == 200, patched.data
    assert (patched.data["net_total"], patched.data["items"][0]["tax_code"]) == ("1525.00", "I0")
    assert buyer.post(f"{ORDERS}{order['id']}/cancel/").data["status"] == "CANCELLED"
    locked = buyer.patch(f"{ORDERS}{order['id']}/", {"items": [
        {"material": "TG-1001", "quantity": "1", "net_price": "1"}]}, format="json")
    assert locked.status_code == 400
    assert buyer.post(f"{ORDERS}{order['id']}/release/").status_code == 400
    assert buyer.delete(f"{ORDERS}{order['id']}/").status_code == 405
    assert buyer.get(ORDERS, {"supplier": demo.supplier.number}).data["count"] == 1


def test_receipt_errors_and_reversal(demo, buyer, warehouse, accountant):
    order = released(buyer, demo)
    url_items = {"purchase_order": order["order_number"], "receipt_date": "2026-05-11"}
    bad = warehouse.post(RECEIPTS, {**url_items, "items": [
        {"item_number": 99, "quantity": "1"}, {"item_number": 10, "quantity": "1", "storage_location": "9999"},
    ]}, format="json")
    assert bad.status_code == 400
    assert bad.data["items"] == [{"item_number": "Not found on this order."},
                                 {"storage_location": "Not found in the item's plant."}]
    too_much = warehouse.post(RECEIPTS, {**url_items, "items": [{"item_number": 10, "quantity": "51"}]},
                              format="json")
    assert "exceeds the open quantity" in str(too_much.data)

    receipt = warehouse.post(RECEIPTS, url_items, format="json").data
    assert warehouse.get(RECEIPTS, {"purchase_order": order["order_number"]}).data["count"] == 1
    reversed_ = warehouse.post(f"{RECEIPTS}{receipt['id']}/reverse/", {"posting_date": "2026-05-12"}, format="json")
    assert (reversed_.status_code, reversed_.data["status"]) == (200, "REVERSED")
    assert warehouse.post(f"{RECEIPTS}{receipt['id']}/reverse/", {}, format="json").status_code == 400
    assert GoodsReceipt.objects.filter(status="REVERSED").count() == 1


def test_invoice_errors_and_cancellation(demo, buyer, warehouse, accountant):
    order = released(buyer, demo)
    base = {"purchase_order": order["order_number"], "supplier_reference": "HS-1", "invoice_date": "2026-05-15"}
    nothing = accountant.post(INVOICES, base, format="json")
    assert nothing.status_code == 400
    assert "Nothing has been received" in str(nothing.data)
    warehouse.post(RECEIPTS, {"purchase_order": order["order_number"], "receipt_date": "2026-05-11"}, format="json")
    unknown = accountant.post(INVOICES, {**base, "items": [{"item_number": 99, "quantity": "1"}]}, format="json")
    assert unknown.data["items"] == [{"item_number": "Not found on this order."}]
    expensive = accountant.post(INVOICES, {**base, "items": [{"item_number": 10, "quantity": "1",
                                                              "net_price": "200"}]}, format="json")
    assert "by more than 5.00%" in str(expensive.data)

    invoice = accountant.post(INVOICES, base, format="json").data
    assert accountant.get(INVOICES, {"supplier": demo.supplier.number, "status": "POSTED"}).data["count"] == 1
    duplicate = accountant.post(INVOICES, base, format="json")
    assert "already posted" in str(duplicate.data)
    cancelled = accountant.post(f"{INVOICES}{invoice['id']}/cancel/", {"posting_date": "2026-05-16"}, format="json")
    assert (cancelled.status_code, cancelled.data["status"], cancelled.data["open_amount"]) == (
        200, "CANCELLED", "0.00",
    )
    assert accountant.post(f"{INVOICES}{invoice['id']}/cancel/", {}, format="json").status_code == 400
    assert accountant.get("/api/purchasing/reports/gr-ir/").status_code == 400  # company_code is required
    assert PurchaseOrder.objects.get().status == "RELEASED"
