from decimal import Decimal

import pytest
from django.urls import reverse

from apps.inventory.models import Product
from apps.sales import services
from apps.sales.models import SalesOrder, SalesOrderLine

pytestmark = pytest.mark.django_db

ORDERS = "/api/sales/orders/"


def detail(order):
    return reverse("sales-order-detail", args=[order.pk])


# --- Creating orders ----------------------------------------------------------------

def test_create_order_with_lines_captures_prices(sales_client, customer, widget, gadget):
    payload = {
        "customer": customer.pk,
        "lines": [
            {"product": widget.pk, "quantity": 3},
            {"product": gadget.pk, "quantity": 1, "unit_price": "20.00"},  # negotiated price
        ],
    }
    response = sales_client.post(ORDERS, payload, format="json")
    assert response.status_code == 201, response.data
    assert response.data["status"] == "PENDING"
    assert Decimal(response.data["total"]) == Decimal("50.00")  # 3 x 10.00 + 1 x 20.00

    order = SalesOrder.objects.get(pk=response.data["id"])
    assert order.created_by == sales_client.user
    assert all(line.created_by == sales_client.user for line in order.lines.all())


def test_line_price_is_kept_when_catalogue_price_changes(pending_order, widget):
    widget.price = Decimal("99.00")
    widget.save()
    line = pending_order.lines.get(product=widget)
    assert line.unit_price == Decimal("10.00")


def test_zero_quantity_rejected(sales_client, customer, widget):
    payload = {"customer": customer.pk, "lines": [{"product": widget.pk, "quantity": 0}]}
    response = sales_client.post(ORDERS, payload, format="json")
    assert response.status_code == 400
    assert not SalesOrder.objects.exists()


def test_new_order_cannot_start_confirmed(sales_client, customer):
    response = sales_client.post(ORDERS, {"customer": customer.pk, "status": "CONFIRMED"}, format="json")
    assert response.status_code == 400
    assert "status" in response.data


def test_order_creation_is_all_or_nothing(sales_client, customer, widget, monkeypatch):
    calls = {"n": 0}
    original = SalesOrderLine.objects.create

    def fail_on_second_line(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure")
        return original(**kwargs)

    monkeypatch.setattr(SalesOrderLine.objects, "create", fail_on_second_line)
    payload = {"customer": customer.pk,
               "lines": [{"product": widget.pk, "quantity": 1}, {"product": widget.pk, "quantity": 2}]}
    with pytest.raises(RuntimeError):
        sales_client.post(ORDERS, payload, format="json")
    assert not SalesOrder.objects.exists()
    assert not SalesOrderLine.objects.exists()


# --- Status changes and stock -------------------------------------------------------

def test_confirm_takes_stock(sales_client, pending_order, widget, gadget):
    response = sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    assert response.status_code == 200, response.data
    widget.refresh_from_db()
    gadget.refresh_from_db()
    assert (widget.stock, gadget.stock) == (90, 3)
    pending_order.refresh_from_db()
    assert pending_order.updated_by == sales_client.user


def test_confirm_with_insufficient_stock_changes_nothing(sales_client, pending_order, widget, gadget):
    Product.objects.filter(pk=gadget.pk).update(stock=1)
    response = sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    assert response.status_code == 400
    assert "G-1" in str(response.data["status"])
    widget.refresh_from_db()
    pending_order.refresh_from_db()
    assert widget.stock == 100  # untouched, even though widget had enough
    assert pending_order.status == "PENDING"


def test_quantities_for_same_product_are_added_together(sales_client, customer, gadget):
    payload = {"customer": customer.pk,
               "lines": [{"product": gadget.pk, "quantity": 3}, {"product": gadget.pk, "quantity": 3}]}
    order_id = sales_client.post(ORDERS, payload, format="json").data["id"]
    response = sales_client.patch(reverse("sales-order-detail", args=[order_id]), {"status": "CONFIRMED"},
                                  format="json")
    assert response.status_code == 400  # needs 6, only 5 in stock


def test_empty_order_cannot_be_confirmed(sales_client, customer):
    order = SalesOrder.objects.create(customer=customer)
    response = sales_client.patch(detail(order), {"status": "CONFIRMED"}, format="json")
    assert response.status_code == 400
    assert "at least one line" in str(response.data["status"])


def test_cancel_confirmed_order_returns_stock(sales_client, pending_order, widget):
    sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    response = sales_client.patch(detail(pending_order), {"status": "CANCELLED"}, format="json")
    assert response.status_code == 200
    widget.refresh_from_db()
    assert widget.stock == 100


def test_cancel_pending_order_leaves_stock_alone(sales_client, pending_order, widget):
    assert sales_client.patch(detail(pending_order), {"status": "CANCELLED"}, format="json").status_code == 200
    widget.refresh_from_db()
    assert widget.stock == 100


def test_ship_confirmed_order(sales_client, pending_order):
    sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    response = sales_client.patch(detail(pending_order), {"status": "SHIPPED"}, format="json")
    assert response.status_code == 200
    assert response.data["status"] == "SHIPPED"


@pytest.mark.parametrize(
    "path, target",
    [
        (["CONFIRMED", "SHIPPED"], "PENDING"),
        (["CONFIRMED", "SHIPPED"], "CANCELLED"),
        (["CANCELLED"], "CONFIRMED"),
        ([], "SHIPPED"),
    ],
)
def test_illegal_transitions_rejected(sales_client, pending_order, path, target):
    for step in path:
        assert sales_client.patch(detail(pending_order), {"status": step}, format="json").status_code == 200
    response = sales_client.patch(detail(pending_order), {"status": target}, format="json")
    assert response.status_code == 400
    assert "status" in response.data


def test_same_status_is_a_no_op(pending_order):
    assert services.change_status(pending_order, "PENDING").status == "PENDING"


# --- Editing rules ------------------------------------------------------------------

def test_lines_can_be_replaced_while_pending(sales_client, pending_order, widget):
    payload = {"lines": [{"product": widget.pk, "quantity": 1}]}
    response = sales_client.patch(detail(pending_order), payload, format="json")
    assert response.status_code == 200
    assert len(response.data["lines"]) == 1
    assert Decimal(response.data["total"]) == Decimal("10.00")


def test_replace_lines_and_confirm_in_one_request_uses_new_lines(sales_client, pending_order, widget, gadget):
    payload = {"lines": [{"product": widget.pk, "quantity": 4}], "status": "CONFIRMED"}
    response = sales_client.patch(detail(pending_order), payload, format="json")
    assert response.status_code == 200, response.data
    widget.refresh_from_db()
    gadget.refresh_from_db()
    assert (widget.stock, gadget.stock) == (96, 5)


def test_lines_locked_after_confirmation(sales_client, pending_order, widget):
    sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    response = sales_client.patch(detail(pending_order), {"lines": [{"product": widget.pk, "quantity": 1}]},
                                  format="json")
    assert response.status_code == 400
    assert "lines" in response.data


def test_customer_locked_after_confirmation(sales_client, pending_order):
    from apps.sales.models import Customer

    other = Customer.objects.create(name="Other", email="other@example.com")
    sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    response = sales_client.patch(detail(pending_order), {"customer": other.pk}, format="json")
    assert response.status_code == 400
    assert "customer" in response.data


def test_put_with_same_customer_allowed_after_confirmation(sales_client, pending_order):
    sales_client.patch(detail(pending_order), {"status": "CONFIRMED"}, format="json")
    response = sales_client.put(detail(pending_order), {"customer": pending_order.customer_id}, format="json")
    assert response.status_code == 200


def test_orders_cannot_be_deleted(sales_client, pending_order):
    assert sales_client.delete(detail(pending_order)).status_code == 405
    assert SalesOrder.objects.filter(pk=pending_order.pk).exists()


def test_order_list_includes_totals(sales_client, pending_order):
    response = sales_client.get(ORDERS)
    assert response.status_code == 200
    order = response.data["results"][0]
    assert Decimal(order["total"]) == Decimal("151.00")  # 10 x 10.00 + 2 x 25.50
    assert order["customer_name"] == "Acme"
