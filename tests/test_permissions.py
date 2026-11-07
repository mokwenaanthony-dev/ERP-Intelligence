"""The role matrix across modules (design document section 3, extended in Waves 1-2c).

A request that passes the permission check returns anything except 403 (often 400 here,
because the test sends an empty body); a request that fails it returns 403.
"""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role

pytestmark = pytest.mark.django_db

ORDERS = "/api/sales/orders/"
PRICES = "/api/sales/prices/"
DELIVERIES = "/api/sales/deliveries/"
INVOICES = "/api/sales/invoices/"
MOVEMENTS = "/api/inventory/goods-movements/"
JOURNAL = "/api/finance/journal-entries/"
PAYMENTS = "/api/finance/incoming-payments/"
MATERIALS = "/api/masterdata/materials/"
PURCHASE_ORDERS = "/api/purchasing/orders/"
RECEIPTS = "/api/purchasing/goods-receipts/"
SUPPLIER_INVOICES = "/api/purchasing/invoices/"
GR_IR = "/api/purchasing/reports/gr-ir/"
SUPPLIER_PAYMENTS = "/api/finance/outgoing-payments/"

R, W = "get", "post"


@pytest.mark.parametrize(
    "role, url, method, allowed",
    [
        # Sales rep: orders, prices and invoices; reads stock; no finance
        (Role.SALES_REP, ORDERS, W, True), (Role.SALES_REP, PRICES, W, True), (Role.SALES_REP, INVOICES, W, True),
        (Role.SALES_REP, DELIVERIES, R, True), (Role.SALES_REP, MOVEMENTS, R, True),
        (Role.SALES_REP, MOVEMENTS, W, False), (Role.SALES_REP, JOURNAL, R, False),
        (Role.SALES_REP, PAYMENTS, W, False),
        # Inventory manager: goods movements and materials; no sales orders or invoices
        (Role.INVENTORY_MANAGER, MOVEMENTS, W, True), (Role.INVENTORY_MANAGER, MATERIALS, W, True),
        (Role.INVENTORY_MANAGER, DELIVERIES, R, True), (Role.INVENTORY_MANAGER, ORDERS, R, False),
        (Role.INVENTORY_MANAGER, INVOICES, R, False),
        # Finance manager: reads sales, bills, takes payments; never moves stock or enters orders
        (Role.FINANCE_MANAGER, ORDERS, R, True), (Role.FINANCE_MANAGER, ORDERS, W, False),
        (Role.FINANCE_MANAGER, INVOICES, W, True), (Role.FINANCE_MANAGER, PAYMENTS, W, True),
        (Role.FINANCE_MANAGER, MOVEMENTS, W, False),
        # Accountant: journal, billing and payments
        (Role.ACCOUNTANT, JOURNAL, W, True), (Role.ACCOUNTANT, INVOICES, W, True),
        (Role.ACCOUNTANT, PAYMENTS, W, True), (Role.ACCOUNTANT, DELIVERIES, R, True),
        # Purchasing officer: orders and receipts; reads stock and supplier invoices; never posts or pays them
        (Role.PURCHASING_OFFICER, PURCHASE_ORDERS, W, True), (Role.PURCHASING_OFFICER, RECEIPTS, W, True),
        (Role.PURCHASING_OFFICER, SUPPLIER_INVOICES, R, True), (Role.PURCHASING_OFFICER, GR_IR, R, True),
        (Role.PURCHASING_OFFICER, SUPPLIER_INVOICES, W, False),
        (Role.PURCHASING_OFFICER, SUPPLIER_PAYMENTS, W, False),
        (Role.PURCHASING_OFFICER, MOVEMENTS, R, True), (Role.PURCHASING_OFFICER, ORDERS, R, False),
        # Procure-to-pay from the other side: the warehouse receives, finance invoices and pays
        (Role.INVENTORY_MANAGER, RECEIPTS, W, True), (Role.INVENTORY_MANAGER, PURCHASE_ORDERS, R, True),
        (Role.INVENTORY_MANAGER, PURCHASE_ORDERS, W, False), (Role.INVENTORY_MANAGER, SUPPLIER_INVOICES, R, False),
        (Role.ACCOUNTANT, SUPPLIER_INVOICES, W, True), (Role.ACCOUNTANT, SUPPLIER_PAYMENTS, W, True),
        (Role.ACCOUNTANT, PURCHASE_ORDERS, R, True), (Role.ACCOUNTANT, PURCHASE_ORDERS, W, False),
        (Role.ACCOUNTANT, RECEIPTS, W, False), (Role.FINANCE_MANAGER, SUPPLIER_INVOICES, W, True),
        (Role.SALES_REP, PURCHASE_ORDERS, R, False), (Role.SALES_REP, SUPPLIER_INVOICES, R, False),
        (Role.VIEWER, PURCHASE_ORDERS, R, True), (Role.VIEWER, PURCHASE_ORDERS, W, False),
        # HR admin: nothing outside HR
        (Role.HR_ADMIN, ORDERS, R, False), (Role.HR_ADMIN, MOVEMENTS, R, False), (Role.HR_ADMIN, JOURNAL, R, False),
        # Viewer: read only
        (Role.VIEWER, ORDERS, R, True), (Role.VIEWER, INVOICES, R, True), (Role.VIEWER, ORDERS, W, False),
        (Role.VIEWER, INVOICES, W, False),
        # Administrator: everything
        (Role.ADMIN, ORDERS, W, True), (Role.ADMIN, JOURNAL, W, True), (Role.ADMIN, PAYMENTS, W, True),
    ],
)
def test_role_matrix(client_for, role, url, method, allowed):
    response = getattr(client_for(role), method)(url, {}, format="json")
    if allowed:
        assert response.status_code != 403, response.data
    else:
        assert response.status_code == 403


def test_superuser_passes_every_check(make_user):
    client = APIClient()
    client.force_authenticate(user=make_user(Role.VIEWER, username="root", is_superuser=True))
    assert client.post(JOURNAL, {}, format="json").status_code == 400  # past the permission check


def test_unauthenticated_requests_are_rejected(anon_client):
    for url in (ORDERS, MOVEMENTS, JOURNAL):
        assert anon_client.get(url).status_code == 401
