"""Django admin: audit fields are filled in, and posted documents are read-only."""
import pytest

from apps.accounts.models import Role
from apps.masterdata.models import BusinessPartner, CustomerRole, Material, UnitOfMeasure

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_browser(client, make_user):
    user = make_user(Role.ADMIN, username="boss", is_staff=True, is_superuser=True)
    client.force_login(user)
    client.user = user
    return client


def test_admin_add_and_change_record_who_did_it(demo, admin_browser, make_user):
    unit = UnitOfMeasure.objects.get(code="EA")
    payload = {"number": "AD-1", "description": "Admin item", "material_type": "HAWA", "base_unit": unit.pk,
               "material_group": "", "is_active": "on"}
    assert admin_browser.post("/admin/masterdata/material/add/", payload).status_code == 302
    material = Material.objects.get(number="AD-1")
    assert material.created_by == material.updated_by == admin_browser.user

    original = make_user(Role.INVENTORY_MANAGER, username="original")
    Material.objects.filter(pk=material.pk).update(created_by=original)
    response = admin_browser.post(f"/admin/masterdata/material/{material.pk}/change/",
                                  {**payload, "description": "Renamed"})
    assert response.status_code == 302
    material.refresh_from_db()
    assert (material.created_by, material.updated_by) == (original, admin_browser.user)


def test_admin_inline_roles_get_audit_fields(demo, admin_browser):
    data = {
        "category": "ORGANIZATION", "name": "Inline Traders", "country": "ZA",
        "customer_roles-TOTAL_FORMS": "1", "customer_roles-INITIAL_FORMS": "0",
        "customer_roles-MIN_NUM_FORMS": "0", "customer_roles-MAX_NUM_FORMS": "1000",
        "customer_roles-0-company_code": demo.company.pk,
        "customer_roles-0-reconciliation_account": demo.account("1100").pk,
        "customer_roles-0-payment_terms_days": "30", "customer_roles-0-credit_limit": "0",
        "supplier_roles-TOTAL_FORMS": "0", "supplier_roles-INITIAL_FORMS": "0",
        "supplier_roles-MIN_NUM_FORMS": "0", "supplier_roles-MAX_NUM_FORMS": "1000",
    }
    response = admin_browser.post("/admin/masterdata/businesspartner/add/", data)
    assert response.status_code == 302, response.content[:3000]
    role = CustomerRole.objects.get(business_partner=BusinessPartner.objects.get(name="Inline Traders"))
    assert role.created_by == admin_browser.user


def test_sales_admin_pages_are_read_only(demo, admin_browser):
    for path in ("salesorder", "delivery", "invoice", "salesprice", "revenueaccountsetting"):
        assert admin_browser.get(f"/admin/sales/{path}/").status_code == 200, path
    for path in ("salesorder", "delivery", "invoice"):
        assert admin_browser.get(f"/admin/sales/{path}/add/").status_code == 403, path
    assert admin_browser.get("/admin/finance/openitemclearing/").status_code == 200
    assert admin_browser.get("/admin/accounts/user/").status_code == 200
