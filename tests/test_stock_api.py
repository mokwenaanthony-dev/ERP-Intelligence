"""Inventory REST API: goods movements, reversal, stock overview, reconciliation and account settings."""
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.inventory.models import MaterialDocument

pytestmark = pytest.mark.django_db

MOVEMENTS = "/api/inventory/goods-movements/"


def receipt_payload(demo, **overrides):
    payload = {
        "movement_type": "101",
        "plant": "1000",
        "posting_date": "2026-05-15",
        "reference": "DN-778",
        "business_partner": demo.supplier.number,
        "items": [{"material": "TG-1001", "storage_location": "0001", "quantity": "50", "unit_price": "150.00"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def storeman(client_for):
    return client_for(Role.INVENTORY_MANAGER)


def test_inventory_manager_posts_goods_receipt(demo, storeman):
    response = storeman.post(MOVEMENTS, receipt_payload(demo), format="json")
    assert response.status_code == 201, response.data
    data = response.data
    assert (data["document_number"], data["journal_entry"], data["movement_type_name"]) == (
        "MD00000002", "WE00000002", "Goods receipt",
    )
    assert data["business_partner"] == demo.supplier.number
    assert data["items"] == [{
        "line_number": 1, "material": "TG-1001", "material_description": "Standard widget",
        "storage_location": "0001", "quantity": "50.000", "unit": "EA", "amount_local": "7500.00", "text": "",
    }]
    assert data["created_by"] == storeman.user.pk


def test_transfer_via_api(demo, storeman):
    payload = {"movement_type": "311", "plant": "1000", "posting_date": "2026-05-15",
               "items": [{"material": "TG-1001", "storage_location": "0001", "to_storage_location": "0002",
                          "quantity": "5"}]}
    response = storeman.post(MOVEMENTS, payload, format="json")
    assert response.status_code == 201, response.data
    assert response.data["journal_entry"] is None
    assert len(response.data["items"]) == 2


def test_unknown_references_reported_per_item(demo, storeman):
    payload = receipt_payload(demo, items=[
        {"material": "NOPE", "storage_location": "9999", "quantity": "1", "unit_price": "1"},
        {"material": "TG-1001", "storage_location": "0001", "to_storage_location": "9999", "quantity": "1"},
    ])
    response = storeman.post(MOVEMENTS, payload, format="json")
    assert response.status_code == 400
    first, second = response.data["items"]
    assert set(first) == {"material", "storage_location"}
    assert set(second) == {"to_storage_location"}


@pytest.mark.parametrize("field, value", [("quantity", "0"), ("unit_price", "-1")])
def test_invalid_item_values(demo, storeman, field, value):
    payload = receipt_payload(demo)
    payload["items"][0][field] = value
    response = storeman.post(MOVEMENTS, payload, format="json")
    assert response.status_code == 400
    assert field in response.data["items"][0]


def test_business_rule_errors_return_400(demo, storeman):
    payload = receipt_payload(demo, movement_type="601", items=[
        {"material": "TG-1001", "storage_location": "0001", "quantity": "500"},
    ])
    response = storeman.post(MOVEMENTS, payload, format="json")
    assert response.status_code == 400
    assert "Insufficient stock" in str(response.data["non_field_errors"])


def test_permissions(demo, client_for):
    sales = client_for(Role.SALES_REP)
    assert sales.get(MOVEMENTS).status_code == 200  # catalogue and stock are readable
    assert sales.post(MOVEMENTS, receipt_payload(demo), format="json").status_code == 403
    assert client_for(Role.HR_ADMIN).get("/api/inventory/stock/").status_code == 403


def test_list_filters(demo, storeman):
    storeman.post(MOVEMENTS, receipt_payload(demo), format="json")
    assert storeman.get(MOVEMENTS).data["count"] == 2
    assert storeman.get(MOVEMENTS, {"movement_type": "101"}).data["count"] == 1
    assert storeman.get(MOVEMENTS, {"plant": "1000", "material": "TG-1002"}).data["count"] == 1
    assert storeman.get(MOVEMENTS, {"status": "REVERSED"}).data["count"] == 0


def test_reverse_via_api(demo, storeman):
    created = storeman.post(MOVEMENTS, receipt_payload(demo), format="json").data
    response = storeman.post(f"{MOVEMENTS}{created['id']}/reverse/", {"posting_date": "2026-05-20"}, format="json")
    assert response.status_code == 201, response.data
    assert response.data["reversal_of"] == created["document_number"]
    assert response.data["posting_date"] == "2026-05-20"
    assert storeman.get(f"{MOVEMENTS}{created['id']}/").data["reversed_by"] == response.data["document_number"]

    again = storeman.post(f"{MOVEMENTS}{created['id']}/reverse/", {}, format="json")
    assert again.status_code == 400
    assert MaterialDocument.objects.filter(status="REVERSED").count() == 1


def test_stock_overview_endpoint(demo, client_for):
    viewer = client_for(Role.VIEWER)
    rows = viewer.get("/api/inventory/stock/", {"plant": "1000"}).data
    assert [row["material"] for row in rows] == ["TG-1001", "TG-1002"]
    assert rows[0]["value"] == Decimal("12000.00")
    assert len(viewer.get("/api/inventory/stock/", {"material": "TG-1002"}).data) == 1
    assert viewer.get("/api/inventory/stock/", {"plant": "9999"}).status_code == 400
    assert viewer.get("/api/inventory/stock/", {"material": "NOPE"}).status_code == 400


def test_reconciliation_endpoint(demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    report = accountant.get("/api/inventory/reports/stock-reconciliation/", {"company_code": "1000"}).data
    assert (report["reconciled"], report["stock_value"]) == (True, Decimal("27000.00"))
    assert accountant.get("/api/inventory/reports/stock-reconciliation/").status_code == 400


def test_account_settings(demo, client_for):
    url = "/api/inventory/account-settings/"
    finance_manager = client_for(Role.FINANCE_MANAGER)
    created = finance_manager.post(url, {"chart_of_accounts": "CAZA", "account_key": "INVENTORY",
                                         "material_type": "ROH", "gl_account": "1200"}, format="json")
    assert created.status_code == 201, created.data
    assert created.data["gl_account"] == "1200"
    assert finance_manager.post(url, {"chart_of_accounts": "CAZA", "account_key": "SCRAP", "material_type": "",
                                      "gl_account": "9999"}, format="json").status_code == 400
    assert finance_manager.post(url, {"chart_of_accounts": "CAZA", "account_key": "SCRAP", "material_type": "XXXX",
                                      "gl_account": "5100"}, format="json").status_code == 400

    accountant = client_for(Role.ACCOUNTANT)
    assert accountant.get(url).data["count"] == 7
    assert accountant.post(url, {}, format="json").status_code == 403


def test_inventory_admin_pages(demo, client, make_user):
    client.force_login(make_user(Role.ADMIN, username="boss", is_staff=True, is_superuser=True))
    document = MaterialDocument.objects.first()
    for url in ("/admin/inventory/materialdocument/", f"/admin/inventory/materialdocument/{document.pk}/change/",
                "/admin/inventory/inventoryaccountsetting/", "/admin/masterdata/materialplant/"):
        assert client.get(url).status_code == 200, url
    assert client.get("/admin/inventory/materialdocument/add/").status_code == 403
