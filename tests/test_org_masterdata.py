"""Organisation structure and master data APIs, plus the demo setup command."""
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role
from apps.finance.management.commands.setup_demo_company import Command as SetupCommand
from apps.finance.models import GLAccount, JournalEntry, TaxCode
from apps.masterdata.models import (
    BusinessPartner,
    CustomerRole,
    ExchangeRate,
    Material,
    SupplierRole,
    UnitOfMeasure,
)
from apps.organization.models import CompanyCode, Plant, PurchasingOrganization, SalesOrganization, StorageLocation

pytestmark = pytest.mark.django_db


# --- Organisation structure -------------------------------------------------------------

def test_everyone_reads_org_structure_but_only_admin_changes_it(demo, client_for):
    sales = client_for(Role.SALES_REP)
    assert sales.get("/api/org/company-codes/1000/").data["currency"] == "ZAR"
    for path, count in (("plants", 1), ("storage-locations", 2), ("sales-organizations", 1),
                        ("purchasing-organizations", 1)):
        assert sales.get(f"/api/org/{path}/").data["count"] == count, path
    assert sales.post("/api/org/plants/", {"code": "2000", "name": "Durban", "company_code": "1000"},
                      format="json").status_code == 403

    admin = client_for(Role.ADMIN)
    created = admin.post("/api/org/plants/", {"code": "2000", "name": "Durban", "company_code": "1000"}, format="json")
    assert created.status_code == 201, created.data
    assert Plant.objects.get(code="2000").created_by == admin.user


def test_admin_creates_company_code(demo, client_for):
    admin = client_for(Role.ADMIN)
    payload = {"code": "2000", "name": "ERP Demo Namibia", "country": "NA", "currency": "ZAR",
               "chart_of_accounts": "CAZA", "fiscal_year_start_month": 1}
    response = admin.post("/api/org/company-codes/", payload, format="json")
    assert response.status_code == 201, response.data
    bad = admin.post("/api/org/company-codes/", {**payload, "code": "too-long"}, format="json")
    assert bad.status_code == 400


def test_company_code_in_use_cannot_be_deleted(demo, client_for):
    assert client_for(Role.ADMIN).delete("/api/org/company-codes/1000/").status_code == 409


# --- Business partners ----------------------------------------------------------------

def test_business_partner_numbers_are_assigned(demo, client_for):
    sales = client_for(Role.SALES_REP)
    response = sales.post("/api/masterdata/business-partners/", {"name": "Karoo Traders", "city": "Cape Town"},
                          format="json")
    assert response.status_code == 201, response.data
    partner = BusinessPartner.objects.get(name="Karoo Traders")
    assert response.data["number"] == partner.number == str(BusinessPartner.NUMBER_BASE + partner.pk)
    assert partner.created_by == sales.user


def test_partner_permissions(demo, client_for):
    inventory = client_for(Role.INVENTORY_MANAGER)
    assert inventory.get("/api/masterdata/business-partners/").status_code == 200
    assert inventory.post("/api/masterdata/business-partners/", {"name": "X"}, format="json").status_code == 403


def test_partner_detail_shows_roles_and_search_works(demo, client_for):
    viewer = client_for(Role.VIEWER)
    detail = viewer.get(f"/api/masterdata/business-partners/{demo.customer.number}/").data
    assert detail["customer_roles"] == [{"company_code": "1000", "reconciliation_account": "1100",
                                         "payment_terms_days": 30}]
    assert detail["supplier_roles"] == []
    assert viewer.get("/api/masterdata/business-partners/", {"search": "highveld"}).data["count"] == 1


def test_customer_role_must_use_receivables_account(demo, client_for):
    finance = client_for(Role.FINANCE_MANAGER)
    partner = BusinessPartner.objects.create(name="New Customer")
    url = "/api/masterdata/customer-roles/"
    base = {"business_partner": partner.number, "company_code": "1000"}

    wrong_type = finance.post(url, {**base, "reconciliation_account": "2000"}, format="json")
    assert wrong_type.status_code == 400
    assert "not a customer reconciliation account" in str(wrong_type.data)
    unknown = finance.post(url, {**base, "reconciliation_account": "9999"}, format="json")
    assert unknown.status_code == 400

    created = finance.post(url, {**base, "reconciliation_account": "1100", "credit_limit": "50000.00"}, format="json")
    assert created.status_code == 201, created.data
    assert created.data["reconciliation_account_number"] == "1100"
    assert partner.has_customer_role(demo.company)
    duplicate = finance.post(url, {**base, "reconciliation_account": "1100"}, format="json")
    assert duplicate.status_code == 400


def test_supplier_role(demo, client_for):
    purchasing = client_for(Role.PURCHASING_OFFICER)
    partner = BusinessPartner.objects.create(name="New Supplier")
    response = purchasing.post(
        "/api/masterdata/supplier-roles/",
        {"business_partner": partner.number, "company_code": "1000", "reconciliation_account": "2000"},
        format="json",
    )
    assert response.status_code == 201, response.data
    assert partner.has_supplier_role(demo.company)
    assert not partner.has_customer_role(demo.company)


# --- Materials and reference data ---------------------------------------------------------

def test_material_permissions_and_lookup(demo, client_for):
    inventory = client_for(Role.INVENTORY_MANAGER)
    payload = {"number": "RM-2001", "description": "Steel sheet", "material_type": "ROH", "base_unit": "KG"}
    assert inventory.post("/api/masterdata/materials/", payload, format="json").status_code == 201
    sales = client_for(Role.SALES_REP)
    assert sales.get("/api/masterdata/materials/RM-2001/").data["base_unit"] == "KG"
    assert sales.post("/api/masterdata/materials/", {**payload, "number": "X"}, format="json").status_code == 403


def test_reference_data(demo, client_for):
    finance = client_for(Role.FINANCE_MANAGER)
    currency = finance.post("/api/masterdata/currencies/", {"code": "BWP", "name": "Pula"}, format="json")
    assert currency.status_code == 201
    assert finance.get("/api/masterdata/units/").data["count"] == 5

    rates = "/api/masterdata/exchange-rates/"
    same = finance.post(rates, {"from_currency": "USD", "to_currency": "USD", "rate": "1", "valid_from": "2026-01-01"},
                        format="json")
    assert same.status_code == 400
    negative = finance.post(rates, {"from_currency": "BWP", "to_currency": "ZAR", "rate": "-1",
                                    "valid_from": "2026-01-01"}, format="json")
    assert negative.status_code == 400
    ok = finance.post(rates, {"from_currency": "BWP", "to_currency": "ZAR", "rate": "1.35",
                              "valid_from": "2026-01-01"}, format="json")
    assert ok.status_code == 201, ok.data

    assert client_for(Role.SALES_REP).post("/api/masterdata/currencies/", {"code": "XXX", "name": "x"},
                                           format="json").status_code == 403
    assert finance.delete("/api/masterdata/currencies/ZAR/").status_code == 409  # used by company code 1000


def test_string_representations(demo):
    assert str(demo.company) == "1000 ERP Demo (Pty) Ltd"
    assert str(Plant.objects.get()) == "1000 Johannesburg"
    assert str(StorageLocation.objects.get(code="0001")) == "1000/0001 Main warehouse"
    assert str(SalesOrganization.objects.get()) == "1000 Sales South Africa"
    assert str(PurchasingOrganization.objects.get()) == "1000 Purchasing South Africa"
    assert str(demo.currency("ZAR")) == "ZAR"
    assert str(Material.objects.get(number="TG-1001")) == "TG-1001 Standard widget"
    assert str(demo.customer).endswith("Acme Retail (Pty) Ltd")
    assert str(CustomerRole.objects.get()).endswith("as customer in 1000")
    assert str(SupplierRole.objects.get()).endswith("as supplier in 1000")
    assert "1 USD = 18.000000 ZAR" in str(ExchangeRate.objects.get(from_currency__code="USD"))
    assert str(demo.account("1000")) == "1000 Bank - current account"
    assert str(demo.chart) == "CAZA Chart of accounts - South Africa"
    assert str(demo.tax("O1")) == "O1 Output VAT 15% (15.00%)"
    assert str(demo.company.fiscal_years.get()) == "1000 FY2027"
    assert str(demo.company.fiscal_years.get().periods.first()) == "1000 FY2027 P01"
    assert str(UnitOfMeasure.objects.get(code="EA")) == "EA"


# --- Demo setup command ----------------------------------------------------------------

def test_setup_demo_company_is_repeatable(demo):
    call_command("setup_demo_company", "--fiscal-year", "2027", stdout=StringIO())
    assert CompanyCode.objects.count() == 1
    assert GLAccount.objects.count() == 28
    assert TaxCode.objects.count() == 5
    assert BusinessPartner.objects.count() == 2
    assert JournalEntry.objects.count() == 3


def test_setup_demo_company_defaults_to_current_fiscal_year(db):
    call_command("setup_demo_company", stdout=StringIO())
    fiscal_year = CompanyCode.objects.get(code="1000").fiscal_years.get()
    assert fiscal_year.start_date <= date.today() <= fiscal_year.end_date


@pytest.mark.parametrize(
    "day, start_month, expected",
    [(date(2026, 11, 6), 3, 2027), (date(2026, 2, 1), 3, 2026), (date(2026, 3, 1), 3, 2027),
     (date(2026, 6, 1), 1, 2026)],
)
def test_fiscal_year_containing(day, start_month, expected):
    assert SetupCommand._fiscal_year_containing(day, start_month) == expected


def test_admin_pages_render(demo, client, make_user):
    user = make_user(Role.ADMIN, username="boss", is_staff=True, is_superuser=True)
    client.force_login(user)
    entry = JournalEntry.objects.first()
    for url in (
        "/admin/organization/companycode/", "/admin/organization/plant/", "/admin/masterdata/businesspartner/",
        f"/admin/masterdata/businesspartner/{demo.customer.pk}/change/", "/admin/masterdata/material/",
        "/admin/finance/glaccount/", "/admin/finance/taxcode/", "/admin/finance/fiscalyear/",
        f"/admin/finance/fiscalyear/{demo.company.fiscal_years.get().pk}/change/",
        "/admin/finance/journalentry/", f"/admin/finance/journalentry/{entry.pk}/change/",
        "/admin/masterdata/exchangerate/", "/admin/masterdata/currency/",
    ):
        assert client.get(url).status_code == 200, url
    assert client.get("/admin/finance/journalentry/add/").status_code == 403
    assert client.get("/admin/finance/fiscalyear/add/").status_code == 403
