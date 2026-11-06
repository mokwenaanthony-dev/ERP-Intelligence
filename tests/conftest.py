from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.inventory.models import Product
from apps.sales.models import Customer, SalesOrder, SalesOrderLine

PASSWORD = "Str0ng-Test-Pass!"  # nosec B105


@pytest.fixture
def make_user(db):
    def _make(role=Role.VIEWER, username=None, **extra):
        username = username or role.lower()
        return User.objects.create_user(username=username, password=PASSWORD, role=role, **extra)

    return _make


@pytest.fixture
def client_for(make_user):
    """APIClient authenticated as a fresh user with the given role."""

    def _client(role):
        client = APIClient()
        user = make_user(role)
        client.force_authenticate(user=user)
        client.user = user
        return client

    return _client


@pytest.fixture
def anon_client():
    return APIClient()


@pytest.fixture
def sales_client(client_for):
    return client_for(Role.SALES_REP)


@pytest.fixture
def inventory_client(client_for):
    return client_for(Role.INVENTORY_MANAGER)


@pytest.fixture
def widget(db):
    return Product.objects.create(name="Widget", sku="W-1", price=Decimal("10.00"), stock=100)


@pytest.fixture
def gadget(db):
    return Product.objects.create(name="Gadget", sku="G-1", price=Decimal("25.50"), stock=5)


@pytest.fixture
def customer(db):
    return Customer.objects.create(name="Acme", email="acme@example.com")


@pytest.fixture
def pending_order(customer, widget, gadget):
    order = SalesOrder.objects.create(customer=customer)
    SalesOrderLine.objects.create(order=order, product=widget, quantity=10)
    SalesOrderLine.objects.create(order=order, product=gadget, quantity=2)
    return order
