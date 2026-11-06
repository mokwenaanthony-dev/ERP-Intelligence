"""Sales REST API: prices, orders, deliveries and invoices, end to end."""
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.sales.models import Delivery, SalesOrder

pytestmark = pytest.mark.django_db

ORDERS = "/api/sales/orders/"


def order_payload(demo, **overrides):
    payload = {
        "sales_organization": "1000", "customer": demo.customer.number, "order_date": "2026-05-10",
        "customer_reference": "PO-4471",
        "items": [{"material": "TG-1001", "quantity": "10"}, {"material": "TG-1002", "quantity": "2"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def sales(client_for):
    return client_for(Role.SALES_REP)


@pytest.fixture
def accountant(client_for):
    return client_for(Role.ACCOUNTANT)


def create_confirmed(sales, demo, **overrides):
    order = sales.post(ORDERS, order_payload(demo, **overrides), format="json").data
    sales.post(f"{ORDERS}{order['id']}/confirm/")
    return order


def test_order_to_invoice_via_api(demo, sales, accountant):
    created = sales.post(ORDERS, order_payload(demo), format="json")
    assert created.status_code == 201, created.data
    order = created.data
    assert (order["order_number"], order["status"], order["net_total"]) == ("SO00000001", "OPEN", "2988.00")
    assert order["items"][0]["net_price"] == "199.00"
    assert order["created_by"] == sales.user.pk

    confirmed = sales.post(f"{ORDERS}{order['id']}/confirm/")
    assert confirmed.data["status"] == "CONFIRMED"

    delivered = sales.post(f"{ORDERS}{order['id']}/deliver/", {"delivery_date": "2026-05-12",
                                                               "storage_location": "0001"}, format="json")
    assert delivered.status_code == 201, delivered.data
    assert (delivered.data["delivery_number"], delivered.data["material_document"],
            delivered.data["goods_issue_journal_entry"]) == ("DL00000001", "MD00000002", "WA00000001")

    invoiced = accountant.post("/api/sales/invoices/", {"delivery": delivered.data["id"],
                                                        "invoice_date": "2026-05-15"}, format="json")
    assert invoiced.status_code == 201, invoiced.data
    assert (invoiced.data["gross_amount"], invoiced.data["open_amount"], invoiced.data["journal_entry"]) == (
        "3436.20", "3436.20", "DR00000002",
    )
    assert invoiced.data["due_date"] == "2026-06-14"
    assert sales.get(f"{ORDERS}{order['id']}/").data["status"] == "COMPLETED"


def test_partial_delivery_by_item(demo, sales):
    order = create_confirmed(sales, demo)
    response = sales.post(f"{ORDERS}{order['id']}/deliver/", {
        "delivery_date": "2026-05-12", "items": [{"item_number": 10, "quantity": "4", "storage_location": "0001"}],
    }, format="json")
    assert response.status_code == 201, response.data
    assert response.data["items"] == [{"item_number": 10, "material": "TG-1001", "storage_location": "0001",
                                       "quantity": "4.000"}]
    detail = sales.get(f"{ORDERS}{order['id']}/").data
    assert detail["items"][0]["open_quantity"] == "6.000"
    assert detail["deliveries"] == ["DL00000001"]


def test_deliver_input_errors(demo, sales):
    order = create_confirmed(sales, demo)
    url = f"{ORDERS}{order['id']}/deliver/"
    bad_items = sales.post(url, {"delivery_date": "2026-05-12", "items": [
        {"item_number": 99, "quantity": "1", "storage_location": "0001"},
        {"item_number": 10, "quantity": "1", "storage_location": "9999"},
    ]}, format="json")
    assert bad_items.status_code == 400
    assert set(bad_items.data["items"][0]) == {"item_number"}
    assert set(bad_items.data["items"][1]) == {"storage_location"}
    assert sales.post(url, {"delivery_date": "2026-05-12", "storage_location": "9999"},
                      format="json").status_code == 400
    too_much = sales.post(url, {"delivery_date": "2026-05-12", "items": [
        {"item_number": 10, "quantity": "50", "storage_location": "0001"}]}, format="json")
    assert too_much.status_code == 400
    assert "exceeds the open quantity" in str(too_much.data)


def test_order_validation_errors(demo, sales):
    bad_tax = order_payload(demo, items=[{"material": "TG-1001", "quantity": "1", "tax_code": "ZZ"}])
    response = sales.post(ORDERS, bad_tax, format="json")
    assert response.status_code == 400
    assert "tax_code" in response.data["items"][0]
    assert sales.post(ORDERS, order_payload(demo, items=[{"material": "TG-1001", "quantity": "0"}]),
                      format="json").status_code == 400
    rule = sales.post(ORDERS, order_payload(demo, customer=demo.supplier.number), format="json")
    assert rule.status_code == 400
    assert "not a customer" in str(rule.data["non_field_errors"])


def test_changing_items_and_cancelling(demo, sales):
    order = sales.post(ORDERS, order_payload(demo), format="json").data
    patched = sales.patch(f"{ORDERS}{order['id']}/", {"items": [
        {"material": "TG-1002", "quantity": "5", "net_price": "450.00", "tax_code": "O0"},
    ]}, format="json")
    assert patched.status_code == 200, patched.data
    assert patched.data["items"][0]["price_overridden"] is True
    assert patched.data["net_total"] == "2250.00"
    assert sales.post(f"{ORDERS}{order['id']}/cancel/").data["status"] == "CANCELLED"
    locked = sales.patch(f"{ORDERS}{order['id']}/", {"items": [{"material": "TG-1001", "quantity": "1"}]},
                         format="json")
    assert locked.status_code == 400
    assert sales.post(f"{ORDERS}{order['id']}/confirm/").status_code == 400


def test_orders_cannot_be_deleted_or_replaced(demo, sales):
    order = sales.post(ORDERS, order_payload(demo), format="json").data
    assert sales.delete(f"{ORDERS}{order['id']}/").status_code == 405
    assert sales.put(f"{ORDERS}{order['id']}/", order_payload(demo), format="json").status_code == 405


def test_list_filters(demo, sales):
    create_confirmed(sales, demo)
    sales.post(ORDERS, order_payload(demo), format="json")
    assert sales.get(ORDERS).data["count"] == 2
    assert sales.get(ORDERS, {"status": "CONFIRMED"}).data["count"] == 1
    assert sales.get(ORDERS, {"customer": demo.customer.number}).data["count"] == 2


def test_delivery_reversal_and_invoice_cancellation_via_api(demo, sales, client_for):
    order = create_confirmed(sales, demo)
    delivery = sales.post(f"{ORDERS}{order['id']}/deliver/", {"delivery_date": "2026-05-12",
                                                              "storage_location": "0001"}, format="json").data
    warehouse = client_for(Role.INVENTORY_MANAGER)
    assert warehouse.get("/api/sales/deliveries/", {"sales_order": order["order_number"]}).data["count"] == 1
    reversed_ = warehouse.post(f"/api/sales/deliveries/{delivery['id']}/reverse/", {}, format="json")
    assert (reversed_.status_code, reversed_.data["status"]) == (200, "REVERSED")
    assert warehouse.post(f"/api/sales/deliveries/{delivery['id']}/reverse/", {}, format="json").status_code == 400

    delivery = sales.post(f"{ORDERS}{order['id']}/deliver/", {"delivery_date": "2026-05-12",
                                                              "storage_location": "0001"}, format="json").data
    invoice = sales.post("/api/sales/invoices/", {"delivery": delivery["id"], "invoice_date": "2026-05-15"},
                         format="json").data
    assert sales.get("/api/sales/invoices/", {"customer": demo.customer.number}).data["count"] == 1
    cancelled = sales.post(f"/api/sales/invoices/{invoice['id']}/cancel/", {"posting_date": "2026-05-16"},
                           format="json")
    assert (cancelled.status_code, cancelled.data["status"], cancelled.data["open_amount"]) == (
        200, "CANCELLED", "0.00",
    )
    assert sales.post(f"/api/sales/invoices/{invoice['id']}/cancel/", {}, format="json").status_code == 400
    assert sales.get("/api/sales/invoices/", {"status": "CANCELLED"}).data["count"] == 1
    assert sales.post("/api/sales/invoices/", {"delivery": delivery["id"], "invoice_date": "2026-05-15"},
                      format="json").status_code == 201  # re-billed
    assert Delivery.objects.count() == 2


def test_double_invoice_rejected(demo, sales):
    order = create_confirmed(sales, demo)
    delivery = sales.post(f"{ORDERS}{order['id']}/deliver/", {"delivery_date": "2026-05-12",
                                                              "storage_location": "0001"}, format="json").data
    payload = {"delivery": delivery["id"], "invoice_date": "2026-05-15"}
    assert sales.post("/api/sales/invoices/", payload, format="json").status_code == 201
    second = sales.post("/api/sales/invoices/", payload, format="json")
    assert second.status_code == 400
    assert "already been invoiced" in str(second.data)


def test_prices_api(demo, sales, client_for):
    created = sales.post("/api/sales/prices/", {"material": "TG-1001", "sales_organization": "1000",
                                                "price": "209.00", "valid_from": "2026-06-01"}, format="json")
    assert created.status_code == 201, created.data
    assert sales.get("/api/sales/prices/", {"material": "TG-1001"}).data["count"] == 2
    assert sales.post("/api/sales/prices/", {"material": "TG-1001", "sales_organization": "1000", "price": "-1",
                                             "valid_from": "2026-06-02"}, format="json").status_code == 400
    assert client_for(Role.VIEWER).get("/api/sales/prices/").status_code == 200
    assert SalesOrder.objects.count() == 0
    assert Decimal(created.data["price"]) == Decimal("209.00")
