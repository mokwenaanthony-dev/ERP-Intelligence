import pytest
from django.urls import reverse

from apps.sales.models import Customer

pytestmark = pytest.mark.django_db


def test_create_and_read_customer(sales_client):
    response = sales_client.post(reverse("customer-list"), {"name": "Beta Ltd", "email": "beta@example.com"},
                                 format="json")
    assert response.status_code == 201
    customer = Customer.objects.get(email="beta@example.com")
    assert customer.created_by == sales_client.user
    assert sales_client.get(reverse("customer-detail", args=[customer.pk])).data["name"] == "Beta Ltd"


def test_duplicate_email_rejected(sales_client, customer):
    response = sales_client.post(reverse("customer-list"), {"name": "Copy", "email": customer.email},
                                 format="json")
    assert response.status_code == 400


def test_update_customer(sales_client, customer):
    response = sales_client.patch(reverse("customer-detail", args=[customer.pk]), {"phone": "011 000 0000"},
                                  format="json")
    assert response.status_code == 200
    customer.refresh_from_db()
    assert customer.updated_by == sales_client.user


def test_delete_customer_without_orders(sales_client, customer):
    assert sales_client.delete(reverse("customer-detail", args=[customer.pk])).status_code == 204


def test_delete_customer_with_orders_blocked(sales_client, pending_order):
    response = sales_client.delete(reverse("customer-detail", args=[pending_order.customer_id]))
    assert response.status_code == 409
