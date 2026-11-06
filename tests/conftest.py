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


# --- Wave 1 fixtures ---------------------------------------------------------------------

@pytest.fixture
def demo(db):
    """South African demo company 1000, fiscal year 2027 (1 March 2026 - 28 February 2027).

    Already contains two postings dated 2026-03-01: SA00000001 (capital) and DR00000001 (invoice with VAT).
    """
    from io import StringIO
    from types import SimpleNamespace

    from django.core.management import call_command

    from apps.finance.models import GLAccount, TaxCode
    from apps.masterdata.models import BusinessPartner, Currency
    from apps.organization.models import CompanyCode

    call_command("setup_demo_company", "--fiscal-year", "2027", stdout=StringIO())
    company = CompanyCode.objects.select_related("currency", "chart_of_accounts").get(code="1000")
    chart = company.chart_of_accounts
    return SimpleNamespace(
        company=company,
        chart=chart,
        account=lambda number: GLAccount.objects.get(chart_of_accounts=chart, number=number),
        tax=lambda code: TaxCode.objects.get(chart_of_accounts=chart, code=code),
        currency=lambda code: Currency.objects.get(code=code),
        customer=BusinessPartner.objects.get(name="Acme Retail (Pty) Ltd"),
        supplier=BusinessPartner.objects.get(name="Highveld Supplies CC"),
    )
