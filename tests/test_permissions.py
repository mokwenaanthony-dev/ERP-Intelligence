"""The role matrix from section 3 of the design document."""
import pytest
from django.urls import reverse

from apps.accounts.models import Role

pytestmark = pytest.mark.django_db

PRODUCTS = "product-list"
ORDERS = "sales-order-list"
CUSTOMERS = "customer-list"


@pytest.mark.parametrize(
    "role, url_name, method, allowed",
    [
        # Sales rep: writes sales, only looks up the catalogue
        (Role.SALES_REP, ORDERS, "post", True),
        (Role.SALES_REP, CUSTOMERS, "post", True),
        (Role.SALES_REP, PRODUCTS, "get", True),
        (Role.SALES_REP, PRODUCTS, "post", False),
        # Inventory manager: inventory only
        (Role.INVENTORY_MANAGER, PRODUCTS, "post", True),
        (Role.INVENTORY_MANAGER, ORDERS, "get", False),
        # Finance manager: reads sales and inventory, never dispatches or edits stock
        (Role.FINANCE_MANAGER, ORDERS, "get", True),
        (Role.FINANCE_MANAGER, ORDERS, "post", False),
        (Role.FINANCE_MANAGER, PRODUCTS, "get", True),
        (Role.FINANCE_MANAGER, PRODUCTS, "post", False),
        # Purchasing officer: catalogue read only
        (Role.PURCHASING_OFFICER, PRODUCTS, "get", True),
        (Role.PURCHASING_OFFICER, ORDERS, "get", False),
        # HR admin: nothing outside HR
        (Role.HR_ADMIN, PRODUCTS, "get", False),
        (Role.HR_ADMIN, ORDERS, "get", False),
        # Viewer: read only
        (Role.VIEWER, ORDERS, "get", True),
        (Role.VIEWER, ORDERS, "post", False),
        # Admin: everything
        (Role.ADMIN, PRODUCTS, "post", True),
        (Role.ADMIN, ORDERS, "post", True),
    ],
)
def test_role_matrix(client_for, customer, role, url_name, method, allowed):
    client = client_for(role)
    payloads = {
        PRODUCTS: {"name": "New", "sku": f"NEW-{role}", "price": "1.00", "stock": 1},
        ORDERS: {"customer": customer.pk},
        CUSTOMERS: {"name": "New Co", "email": f"{role.lower()}@new.example.com"},
    }
    response = getattr(client, method)(reverse(url_name), payloads[url_name] if method == "post" else None,
                                       format="json")
    if allowed:
        assert response.status_code in (200, 201), response.data
    else:
        assert response.status_code == 403


def test_superuser_passes_every_check(make_user, customer):
    from rest_framework.test import APIClient

    user = make_user(Role.VIEWER, username="root", is_superuser=True)
    client = APIClient()
    client.force_authenticate(user=user)
    assert client.post(reverse(ORDERS), {"customer": customer.pk}, format="json").status_code == 201
