"""Finance REST API: journal posting and reversal, periods, configuration and the trial balance."""
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.finance.models import JournalEntry, PostingPeriod

pytestmark = pytest.mark.django_db

JOURNAL = "/api/finance/journal-entries/"
TRIAL_BALANCE = "/api/finance/reports/trial-balance/"


def invoice_payload(demo, **overrides):
    payload = {
        "company_code": "1000",
        "document_type": "DR",
        "posting_date": "2026-05-15",
        "reference": "INV-0042",
        "header_text": "Widgets for Acme",
        "lines": [
            {"gl_account": "1100", "debit_credit": "D", "amount": "2300.00", "business_partner": demo.customer.number},
            {"gl_account": "4000", "debit_credit": "C", "amount": "2000.00", "tax_code": "O1",
             "material": "TG-1001", "plant": "1000", "text": "20 x widget"},
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def accountant(client_for):
    return client_for(Role.ACCOUNTANT)


@pytest.fixture
def finance_manager(client_for):
    return client_for(Role.FINANCE_MANAGER)


# --- Posting ------------------------------------------------------------------------

def test_accountant_posts_invoice_with_automatic_vat(demo, accountant):
    response = accountant.post(JOURNAL, invoice_payload(demo), format="json")
    assert response.status_code == 201, response.data
    data = response.data
    assert (data["document_number"], data["fiscal_year"], data["period"], data["status"]) == (
        "DR00000002", 2027, 3, "POSTED",
    )
    assert data["created_by"] == accountant.user.pk
    lines = data["lines"]
    assert [(line["gl_account"], line["amount"], line["is_tax_line"]) for line in lines] == [
        ("1100", "2300.00", False),
        ("4000", "-2000.00", False),
        ("2100", "-300.00", True),
    ]
    assert (lines[1]["material"], lines[1]["plant"], lines[1]["tax_code"]) == ("TG-1001", "1000", "O1")
    assert lines[0]["business_partner"] == demo.customer.number


def test_unbalanced_entry_returns_400(demo, accountant):
    payload = invoice_payload(demo)
    payload["lines"][0]["amount"] = "2000.00"
    response = accountant.post(JOURNAL, payload, format="json")
    assert response.status_code == 400
    assert "do not balance" in str(response.data["non_field_errors"])


def test_unknown_references_are_reported_per_line(demo, accountant):
    payload = invoice_payload(demo)
    payload["lines"][0].update({"gl_account": "9999", "business_partner": "0000000"})
    payload["lines"][1].update({"tax_code": "XX", "material": "NOPE", "plant": "9999"})
    response = accountant.post(JOURNAL, payload, format="json")
    assert response.status_code == 400
    first, second = response.data["lines"]
    assert set(first) == {"gl_account", "business_partner"}
    assert set(second) == {"tax_code", "material", "plant"}


def test_zero_amount_rejected_by_api(demo, accountant):
    payload = invoice_payload(demo)
    payload["lines"][0]["amount"] = "0"
    assert accountant.post(JOURNAL, payload, format="json").status_code == 400


def test_sales_rep_has_no_access_to_journal(demo, client_for):
    sales = client_for(Role.SALES_REP)
    assert sales.get(JOURNAL).status_code == 403
    assert sales.post(JOURNAL, invoice_payload(demo), format="json").status_code == 403


def test_journal_cannot_be_edited_or_deleted_via_api(demo, accountant):
    entry = JournalEntry.objects.first()
    assert accountant.patch(f"{JOURNAL}{entry.pk}/", {"header_text": "x"}, format="json").status_code == 405
    assert accountant.delete(f"{JOURNAL}{entry.pk}/").status_code == 405


def test_list_filters(demo, accountant):
    assert accountant.get(JOURNAL, {"company_code": "1000", "document_type": "DR"}).data["count"] == 1
    assert accountant.get(JOURNAL, {"fiscal_year": 2027, "period": 1}).data["count"] == 3  # incl. opening stock
    assert accountant.get(JOURNAL, {"status": "REVERSED"}).data["count"] == 0
    assert accountant.get(JOURNAL, {"fiscal_year": "abc"}).status_code == 400


def test_reverse_via_api(demo, accountant):
    entry = JournalEntry.objects.get(document_number="SA00000001")
    response = accountant.post(f"{JOURNAL}{entry.pk}/reverse/", {"posting_date": "2026-03-31"}, format="json")
    assert response.status_code == 201, response.data
    assert response.data["reversal_of"] == "SA00000001"
    assert response.data["document_number"] == "SA00000002"

    original = accountant.get(f"{JOURNAL}{entry.pk}/").data
    assert (original["status"], original["reversed_by"]) == ("REVERSED", "SA00000002")

    again = accountant.post(f"{JOURNAL}{entry.pk}/reverse/", {}, format="json")
    assert again.status_code == 400
    assert "already been reversed" in str(again.data)


def test_posting_into_closed_period_via_api(demo, accountant):
    PostingPeriod.objects.filter(fiscal_year__year=2027, number=3).update(status="CLOSED")
    response = accountant.post(JOURNAL, invoice_payload(demo), format="json")
    assert response.status_code == 400
    assert "is closed" in str(response.data)


# --- Trial balance -------------------------------------------------------------------

def test_trial_balance_endpoint(demo, accountant):
    response = accountant.get(TRIAL_BALANCE, {"company_code": "1000", "fiscal_year": 2027})
    assert response.status_code == 200
    assert response.data["balanced"] is True
    assert response.data["currency"] == "ZAR"
    assert response.data["total_debit"] == Decimal("538500.00")


@pytest.mark.parametrize(
    "params",
    [{}, {"company_code": "1000"}, {"company_code": "9999", "fiscal_year": 2027},
     {"company_code": "1000", "fiscal_year": "x"}],
)
def test_trial_balance_bad_parameters(demo, accountant, params):
    assert accountant.get(TRIAL_BALANCE, params).status_code == 400


# --- Fiscal years and periods ----------------------------------------------------------

def test_finance_manager_creates_fiscal_year_with_periods(demo, finance_manager):
    response = finance_manager.post("/api/finance/fiscal-years/", {"company_code": "1000", "year": 2028},
                                    format="json")
    assert response.status_code == 201, response.data
    assert (response.data["start_date"], response.data["end_date"]) == ("2027-03-01", "2028-02-29")
    assert len(response.data["periods"]) == 12

    duplicate = finance_manager.post("/api/finance/fiscal-years/", {"company_code": "1000", "year": 2028},
                                     format="json")
    assert duplicate.status_code == 400


def test_accountant_reads_but_cannot_configure(demo, accountant):
    assert accountant.get("/api/finance/fiscal-years/", {"company_code": "1000"}).data["count"] == 1
    assert accountant.post("/api/finance/fiscal-years/", {"company_code": "1000", "year": 2028},
                           format="json").status_code == 403
    period = PostingPeriod.objects.first()
    assert accountant.post(f"/api/finance/periods/{period.pk}/close/").status_code == 403


def test_finance_manager_closes_and_reopens_period(demo, finance_manager):
    periods = finance_manager.get("/api/finance/periods/", {"company_code": "1000", "fiscal_year": 2027}).data
    assert periods["count"] == 12
    period_id = periods["results"][0]["id"]

    closed = finance_manager.post(f"/api/finance/periods/{period_id}/close/")
    assert (closed.status_code, closed.data["status"]) == (200, "CLOSED")
    assert closed.data["updated_by"] == finance_manager.user.pk
    reopened = finance_manager.post(f"/api/finance/periods/{period_id}/open/")
    assert reopened.data["status"] == "OPEN"


# --- Chart of accounts and tax codes -----------------------------------------------------

def test_gl_account_crud_and_protection(demo, finance_manager, accountant):
    payload = {"chart_of_accounts": "CAZA", "number": "6300", "name": "Insurance", "account_type": "EXPENSE"}
    created = finance_manager.post("/api/finance/gl-accounts/", payload, format="json")
    assert created.status_code == 201, created.data
    assert created.data["is_balance_sheet"] is False
    assert accountant.post("/api/finance/gl-accounts/", {**payload, "number": "6400"},
                           format="json").status_code == 403
    assert finance_manager.get("/api/finance/gl-accounts/", {"chart_of_accounts": "CAZA"}).data["count"] == 29

    used = demo.account("1000")
    assert finance_manager.delete(f"/api/finance/gl-accounts/{used.pk}/").status_code == 409
    assert finance_manager.delete(f"/api/finance/gl-accounts/{created.data['id']}/").status_code == 204


def test_tax_code_rules(demo, finance_manager):
    url = "/api/finance/tax-codes/"
    base = {"chart_of_accounts": "CAZA", "code": "O2", "description": "Test VAT", "tax_type": "OUTPUT"}
    assert finance_manager.post(url, {**base, "rate": "10.00"}, format="json").status_code == 400  # no account
    assert finance_manager.post(url, {**base, "rate": "10.00", "gl_account": "9999"},
                                format="json").status_code == 400
    created = finance_manager.post(url, {**base, "rate": "10.00", "gl_account": "2100"}, format="json")
    assert created.status_code == 201, created.data
    assert created.data["gl_account"] == "2100"
    zero = finance_manager.post(url, {**base, "code": "OZ", "rate": "0.00"}, format="json")
    assert (zero.status_code, zero.data["gl_account"]) == (201, None)


def test_chart_of_accounts_lookup_by_code(demo, finance_manager):
    response = finance_manager.get("/api/finance/charts-of-accounts/CAZA/")
    assert (response.status_code, response.data["name"]) == (200, "Chart of accounts - South Africa")
