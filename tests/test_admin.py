import pytest

from apps.accounts.models import Role
from apps.inventory.models import Product
from apps.sales.models import Customer

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_browser(client, make_user):
    user = make_user(Role.ADMIN, username="boss", is_staff=True, is_superuser=True)
    client.force_login(user)
    client.user = user
    return client


def test_admin_add_records_creator(admin_browser):
    response = admin_browser.post(
        "/admin/inventory/product/add/", {"name": "Admin Bolt", "sku": "AB-1", "price": "3.00", "stock": "7"}
    )
    assert response.status_code == 302, response.content[:500]
    product = Product.objects.get(sku="AB-1")
    assert product.created_by == admin_browser.user
    assert product.updated_by == admin_browser.user


def test_admin_change_keeps_creator(admin_browser, make_user, widget):
    original = make_user(Role.INVENTORY_MANAGER, username="original")
    Product.objects.filter(pk=widget.pk).update(created_by=original)
    response = admin_browser.post(
        f"/admin/inventory/product/{widget.pk}/change/",
        {"name": "Renamed", "sku": "W-1", "price": "10.00", "stock": "100"},
    )
    assert response.status_code == 302
    widget.refresh_from_db()
    assert widget.created_by == original
    assert widget.updated_by == admin_browser.user


def test_admin_order_inline_lines_get_audit_fields(admin_browser, customer, widget):
    data = {
        "customer": customer.pk,
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        "lines-0-product": widget.pk,
        "lines-0-quantity": "4",
    }
    response = admin_browser.post("/admin/sales/salesorder/add/", data)
    assert response.status_code == 302, response.content[:2000]
    order = customer.orders.get()
    line = order.lines.get()
    assert line.created_by == admin_browser.user
    assert line.unit_price == widget.price


def test_admin_list_pages_render(admin_browser, pending_order):
    for url in ("/admin/inventory/product/", "/admin/sales/salesorder/", "/admin/sales/customer/",
                "/admin/accounts/user/"):
        assert admin_browser.get(url).status_code == 200, url
    assert Customer.objects.exists()
