"""Customer open items, incoming payments with clearing, ageing and reconciliation.

The demo company starts with one open receivable: DR00000001, R11,500 for Acme Retail,
posted 2026-03-01 and due 2026-03-31 (30-day terms).
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.finance import services as finance
from apps.finance.models import OpenItemClearing
from apps.finance.services import LineInput, PostingError

pytestmark = pytest.mark.django_db

PAY_DAY = date(2026, 6, 10)


def pay(demo, amount, allocations=None, **kwargs):
    kwargs.setdefault("customer", demo.customer)
    return finance.post_incoming_payment(company_code=demo.company, payment_date=PAY_DAY, amount=Decimal(amount),
                                         bank_account=demo.account("1000"), allocations=allocations, **kwargs)


def second_invoice(demo, amount="2300.00", day=date(2026, 5, 1)):
    finance.post_journal_entry(company_code=demo.company, document_type="DR", posting_date=day, lines=[
        LineInput(demo.account("1100"), "D", Decimal(amount), business_partner=demo.customer),
        LineInput(demo.account("4000"), "C", Decimal(amount)),
    ])


def open_amounts(demo):
    return [(item["document_number"], item["open_amount"]) for item in finance.open_items(demo.company)]


def test_demo_starts_with_one_open_item(demo):
    [item] = finance.open_items(demo.company)
    assert (item["document_number"], item["open_amount"], item["due_date"]) == (
        "DR00000001", Decimal("11500.00"), date(2026, 3, 31),
    )
    assert item["business_partner"] == demo.customer.number


def test_payment_without_allocations_clears_oldest_first(demo):
    second_invoice(demo)
    entry = pay(demo, "12000.00")
    assert open_amounts(demo) == [("DR00000002", Decimal("1800.00"))]
    assert [(c.item.entry.document_number, c.amount) for c in entry.clearings_made.all()] == [
        ("DR00000001", Decimal("11500.00")), ("DR00000002", Decimal("500.00")),
    ]


def test_explicit_allocation(demo):
    second_invoice(demo)
    newer = finance.open_items(demo.company)[1]
    pay(demo, "2300.00", allocations=[(newer["line_id"], Decimal("2300.00"))])
    assert open_amounts(demo) == [("DR00000001", Decimal("11500.00"))]


def test_cleared_items_can_still_be_listed(demo):
    pay(demo, "11500.00")
    assert finance.open_items(demo.company) == []
    [item] = finance.open_items(demo.company, include_cleared=True)
    assert (item["cleared"], item["open_amount"]) == (Decimal("11500.00"), Decimal("0.00"))


def test_reversing_a_payment_reopens_the_items(demo):
    entry = pay(demo, "11500.00")
    finance.reverse_journal_entry(entry)
    assert open_amounts(demo) == [("DR00000001", Decimal("11500.00"))]
    assert finance.receivables_reconciliation(demo.company)["reconciled"] is True


def test_reversed_invoices_are_not_open_items(demo):
    from apps.finance.models import JournalEntry

    finance.reverse_journal_entry(JournalEntry.objects.get(document_number="DR00000001"))
    assert finance.open_items(demo.company) == []
    assert finance.receivables_reconciliation(demo.company)["ledger_balance"] == 0


@pytest.mark.parametrize(
    "amount, allocation, message",
    [
        ("0", None, "greater than zero"),
        ("20000.00", None, "fully allocated"),
        ("100.00", "too_much", "between 0.01 and its open amount"),
        ("100.00", "unknown", "is not an open item"),
        ("100.00", "short", "fully allocated"),
    ],
)
def test_payment_rules(demo, amount, allocation, message):
    line_id = finance.open_items(demo.company)[0]["line_id"]
    allocations = {
        None: None,
        "too_much": [(line_id, Decimal("20000.00"))],
        "unknown": [(999999, Decimal("100.00"))],
        "short": [(line_id, Decimal("50.00"))],
    }[allocation]
    with pytest.raises(PostingError, match=message):
        pay(demo, amount, allocations)
    assert OpenItemClearing.objects.count() == 0


def test_payment_needs_a_customer_and_a_bank_account(demo):
    with pytest.raises(PostingError, match="not a customer"):
        pay(demo, "10.00", customer=demo.supplier)
    with pytest.raises(PostingError, match="cannot be used as the bank account"):
        finance.post_incoming_payment(company_code=demo.company, customer=demo.customer, payment_date=PAY_DAY,
                                      amount=Decimal("10"), bank_account=demo.account("1100"))


def test_ageing_buckets(demo):
    second_invoice(demo, day=date(2026, 6, 1))  # due 2026-07-01
    report = finance.receivables_ageing(demo.company, date(2026, 7, 20))
    [acme] = report["customers"]
    assert acme["over_90"] == Decimal("11500.00")  # due 2026-03-31, 111 days overdue
    assert acme["days_1_30"] == Decimal("2300.00")  # 19 days overdue
    assert acme["total"] == report["totals"]["total"] == Decimal("13800.00")
    assert finance.receivables_ageing(demo.company, date(2026, 3, 15))["totals"]["not_due"] == Decimal("11500.00")
    assert finance.receivables_ageing(demo.company, date(2026, 2, 1))["totals"]["total"] == 0  # not yet posted


@pytest.mark.parametrize("days, bucket", [(0, "not_due"), (1, "days_1_30"), (30, "days_1_30"),
                                          (31, "days_31_60"), (61, "days_61_90"), (91, "over_90")])
def test_bucket_boundaries(days, bucket):
    assert finance._bucket(days) == bucket


def test_manual_posting_to_receivables_without_clearing_shows_in_reconciliation(demo):
    finance.post_journal_entry(company_code=demo.company, posting_date=PAY_DAY, lines=[
        LineInput(demo.account("1000"), "D", Decimal("50")),
        LineInput(demo.account("1100"), "C", Decimal("50"), business_partner=demo.customer),
    ])
    report = finance.receivables_reconciliation(demo.company)
    assert (report["reconciled"], report["difference"]) == (False, Decimal("50.00"))


def test_clearing_string(demo):
    pay(demo, "100.00")
    assert str(OpenItemClearing.objects.get()).startswith("1000/2027/DZ00000001 clears 100.00 of")


# --- API --------------------------------------------------------------------------------------

def test_incoming_payment_api(demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    payload = {"company_code": "1000", "customer": demo.customer.number, "payment_date": "2026-06-10",
               "amount": "500.00", "bank_account": "1000", "reference": "EFT-1"}
    response = accountant.post("/api/finance/incoming-payments/", payload, format="json")
    assert response.status_code == 201, response.data
    assert response.data["document_number"] == "DZ00000001"
    assert response.data["cleared_items"][0]["document_number"] == "DR00000001"

    line_id = finance.open_items(demo.company)[0]["line_id"]
    explicit = {**payload, "allocations": [{"line_id": line_id, "amount": "100.00"}], "amount": "100.00"}
    assert accountant.post("/api/finance/incoming-payments/", explicit, format="json").status_code == 201
    assert accountant.post("/api/finance/incoming-payments/", {**payload, "bank_account": "9999"},
                           format="json").status_code == 400
    assert accountant.post("/api/finance/incoming-payments/", {**payload, "amount": "99999"},
                           format="json").status_code == 400


def test_receivables_report_endpoints(demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    items = accountant.get("/api/finance/reports/open-items/", {"company_code": "1000"}).data
    assert [i["document_number"] for i in items] == ["DR00000001"]
    filtered = accountant.get("/api/finance/reports/open-items/",
                              {"company_code": "1000", "business_partner": demo.supplier.number}).data
    assert filtered == []
    assert accountant.get("/api/finance/reports/open-items/",
                          {"company_code": "1000", "include_cleared": "true"}).status_code == 200
    assert accountant.get("/api/finance/reports/open-items/",
                          {"company_code": "1000", "business_partner": "0"}).status_code == 400
    assert accountant.get("/api/finance/reports/open-items/").status_code == 400

    ageing = accountant.get("/api/finance/reports/receivables-ageing/",
                            {"company_code": "1000", "as_of": "2026-07-20"}).data
    assert ageing["totals"]["over_90"] == Decimal("11500.00")
    assert accountant.get("/api/finance/reports/receivables-ageing/", {"company_code": "1000"}).status_code == 200

    reconciliation = accountant.get("/api/finance/reports/receivables-reconciliation/",
                                    {"company_code": "1000"}).data
    assert reconciliation["reconciled"] is True
