from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User

PASSWORD = "Str0ng-Test-Pass!"  # nosec B105


class IdempotentAPIClient(APIClient):
    """A well-behaved client (ADR 0010): a fresh Idempotency-Key on every request that changes data,
    unless the test gives one itself to exercise retries."""

    def generic(self, method, path, *args, **kwargs):
        headers = kwargs.get("headers") or {}
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and "HTTP_IDEMPOTENCY_KEY" not in kwargs \
                and "Idempotency-Key" not in headers:
            kwargs["HTTP_IDEMPOTENCY_KEY"] = uuid4().hex
        return super().generic(method, path, *args, **kwargs)


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
        client = IdempotentAPIClient()
        user = make_user(role)
        client.force_authenticate(user=user)
        client.user = user
        return client

    return _client


@pytest.fixture
def anon_client():
    return IdempotentAPIClient()


@pytest.fixture
def sales_client(client_for):
    return client_for(Role.SALES_REP)


@pytest.fixture
def inventory_client(client_for):
    return client_for(Role.INVENTORY_MANAGER)


# --- Wave 1 fixtures ---------------------------------------------------------------------

@pytest.fixture
def demo(db):
    """South African demo company 1000, fiscal year 2027 (1 March 2026 - 28 February 2027).

    Already contains, dated 2026-03-01: SA00000001 (capital), DR00000001 (invoice with VAT) and
    opening stock MD00000001 / WE00000001: 100 x TG-1001 @ R120 and 50 x TG-1002 @ R300 in 1000/0001.
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


# --- Wave 9a/9b: the control-tower scenario, shared by test_control and test_decisions -------------------

@pytest.fixture
def tower(demo):
    """The demo company judged on 15 November 2026 (see tests/test_control.py for the story).

    Two customer order lines due in the last 90 days (one on time, one late), two purchase lines (one
    received on time, one overdue), press PRESS3 down, an urgent ticket overdue, steel single source.
    """
    from datetime import date, datetime, timedelta, timezone as dt_timezone
    from decimal import Decimal as D
    from io import StringIO

    from django.core.management import call_command

    from apps.crm import services as crm
    from apps.masterdata.models import Material
    from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization, StorageLocation
    from apps.planning.models import MaterialPlanning
    from apps.production.models import CellState, WorkCenter
    from apps.purchasing import services as purchasing
    from apps.sales import services as sales

    now = datetime(2026, 11, 15, 8, 0, tzinfo=dt_timezone.utc)
    call_command("setup_demo_production", stdout=StringIO())
    plant = Plant.objects.get(code="1000")
    main = StorageLocation.objects.get(plant=plant, code="0001")
    widget = Material.objects.get(number="TG-1001")
    org, sorg = PurchasingOrganization.objects.get(code="1000"), SalesOrganization.objects.get(code="1000")

    # Stock to sell: 100 widgets received on 1 October, on time (due 1 October).
    on_time = purchasing.create_purchase_order(
        purchasing_organization=org, supplier=demo.supplier, order_date=date(2026, 9, 20),
        delivery_date=date(2026, 10, 1), items=[purchasing.OrderItemInput(widget, D("100"), D("180"), plant)])
    purchasing.post_goods_receipt(purchasing.release_order(on_time), receipt_date=date(2026, 10, 1))
    # A second purchase order, due 1 November, never received.
    late_po = purchasing.create_purchase_order(
        purchasing_organization=org, supplier=demo.supplier, order_date=date(2026, 10, 20),
        delivery_date=date(2026, 11, 1), items=[purchasing.OrderItemInput(widget, D("50"), D("180"), plant)])
    purchasing.release_order(late_po)

    # Customer order 1, due 10 October, delivered on 8 October; order 2, due 5 November, not delivered.
    first = sales.create_sales_order(sales_organization=sorg, customer=demo.customer, order_date=date(2026, 10, 1),
                                     requested_delivery_date=date(2026, 10, 10),
                                     items=[sales.OrderItemInput(material=widget, quantity=D("40"), plant=plant)])
    sales.confirm_order(first)
    sales.post_delivery(first, delivery_date=date(2026, 10, 8), storage_location=main)
    late_so = sales.create_sales_order(sales_organization=sorg, customer=demo.customer, order_date=date(2026, 10, 20),
                                       requested_delivery_date=date(2026, 11, 5),
                                       items=[sales.OrderItemInput(material=widget, quantity=D("10"), plant=plant)])

    # The shop floor and service.
    press = WorkCenter.objects.get(plant=plant, code="PRESS3")
    CellState.objects.create(work_center=press, state=CellState.State.DOWN, since=now - timedelta(hours=2),
                             reported_at=now - timedelta(hours=2))
    crm.open_ticket(company_code=demo.company, customer=demo.customer, subject="Bracket cracked",
                    priority="URGENT", opened_at=now - timedelta(days=1))

    # Steel only from Highveld, and an order for brackets that need it.
    MaterialPlanning.objects.create(material=Material.objects.get(number="RM-2001"), plant=plant, procurement="BUY",
                                    lead_time_days=10, preferred_supplier=demo.supplier)
    sales.create_sales_order(sales_organization=sorg, customer=demo.customer, order_date=date(2026, 11, 15),
                             requested_delivery_date=date(2026, 12, 15),
                             items=[sales.OrderItemInput(material=Material.objects.get(number="FG-4001"),
                                                         quantity=D("10"), plant=plant)])
    demo.late_so, demo.late_po = late_so, late_po
    return demo
