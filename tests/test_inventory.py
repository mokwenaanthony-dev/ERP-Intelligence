import pytest
from django.urls import reverse

from apps.inventory.models import Product

pytestmark = pytest.mark.django_db


def test_create_product_records_who_created_it(inventory_client):
    response = inventory_client.post(
        reverse("product-list"), {"name": "Bolt", "sku": "B-1", "price": "2.50", "stock": 40}, format="json"
    )
    assert response.status_code == 201
    product = Product.objects.get(sku="B-1")
    assert product.created_by == inventory_client.user
    assert product.updated_by == inventory_client.user
    assert response.data["created_by"] == inventory_client.user.pk


def test_update_records_who_changed_it(inventory_client, make_user, widget):
    response = inventory_client.patch(reverse("product-detail", args=[widget.pk]), {"price": "12.00"}, format="json")
    assert response.status_code == 200
    widget.refresh_from_db()
    assert str(widget.price) == "12.00"
    assert widget.updated_by == inventory_client.user


def test_audit_fields_cannot_be_set_by_client(inventory_client, make_user):
    other = make_user(username="someone-else")
    inventory_client.post(
        reverse("product-list"),
        {"name": "Nut", "sku": "N-1", "price": "1.00", "created_by": other.pk},
        format="json",
    )
    assert Product.objects.get(sku="N-1").created_by == inventory_client.user


@pytest.mark.parametrize("field, value", [("stock", -1), ("price", "-0.01")])
def test_negative_stock_or_price_rejected(inventory_client, field, value):
    payload = {"name": "Bad", "sku": "BAD", "price": "1.00", "stock": 1, field: value}
    response = inventory_client.post(reverse("product-list"), payload, format="json")
    assert response.status_code == 400
    assert field in response.data


def test_duplicate_sku_rejected(inventory_client, widget):
    response = inventory_client.post(
        reverse("product-list"), {"name": "Copy", "sku": widget.sku, "price": "1.00"}, format="json"
    )
    assert response.status_code == 400


def test_delete_unused_product(inventory_client, widget):
    assert inventory_client.delete(reverse("product-detail", args=[widget.pk])).status_code == 204
    assert not Product.objects.filter(pk=widget.pk).exists()


def test_delete_product_used_on_an_order_is_blocked(inventory_client, pending_order, widget):
    response = inventory_client.delete(reverse("product-detail", args=[widget.pk]))
    assert response.status_code == 409
    assert Product.objects.filter(pk=widget.pk).exists()


def test_product_list_is_paginated(inventory_client, widget, gadget):
    response = inventory_client.get(reverse("product-list"))
    assert response.status_code == 200
    assert response.data["count"] == 2
    assert [p["sku"] for p in response.data["results"]] == ["G-1", "W-1"]
