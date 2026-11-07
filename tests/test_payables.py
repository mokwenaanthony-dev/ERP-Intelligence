"""Supplier open items, outgoing payments with clearing, payables ageing and reconciliation.

Payables are credit items. Each test posts supplier invoices directly as KR journal entries
(Cr 2000 payables / Dr 6100 expense) so the finance rules are tested on their own.
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.finance import services as finance
from apps.finance.models import OpenItemClearing
from apps.finance.services import LineInput, PostingError

pytestmark = pytest.mark.django_db

SUPPLIER = "SUPPLIER"
PAY_DAY = date(2026, 6, 10)


def supplier_invoice(demo, amount="4600.00", day=date(2026, 4, 1)):
    return finance.post_journal_entry(company_code=demo.company, document_type="KR", posting_date=day, lines=[
        LineInput(demo.account("2000"), "C", Decimal(amount), business_partner=demo.supplier),
        LineInput(demo.account("6100"), "D", Decimal(amount)),
    ])


def pay(demo, amount, allocations=None, **kwargs):
    kwargs.setdefault("supplier", demo.supplier)
    return finance.post_outgoing_payment(company_code=demo.company, payment_date=PAY_DAY, amount=Decimal(amount),
                                         bank_account=demo.account("1000"), allocations=allocations, **kwargs)


def open_amounts(demo):
    return [(i["document_number"], i["open_amount"])
            for i in finance.open_items(demo.company, reconciliation_type=SUPPLIER)]


def test_supplier_invoices_are_open_items(demo):
    supplier_invoice(demo)
    [item] = finance.open_items(demo.company, reconciliation_type=SUPPLIER)
    assert (item["document_number"], item["open_amount"], item["due_date"], item["business_partner"]) == (
        "KR00000001", Decimal("4600.00"), date(2026, 5, 1), demo.supplier.number,
    )
    assert finance.open_items(demo.company, reconciliation_type=SUPPLIER, partner=demo.customer) == []


def test_outgoing_payment_clears_oldest_first(demo):
    supplier_invoice(demo)
    supplier_invoice(demo, "1150.00", date(2026, 5, 1))
    entry = pay(demo, "5000.00")
    assert (entry.document_number, entry.document_type) == ("KZ00000001", "KZ")
    assert [(line.gl_account.number, line.amount) for line in entry.lines.order_by("line_number")] == [
        ("2000", Decimal("5000.00")), ("1000", Decimal("-5000.00")),
    ]
    assert entry.header_text == "Payment to Highveld Supplies CC"
    assert open_amounts(demo) == [("KR00000002", Decimal("750.00"))]
    assert finance.payables_reconciliation(demo.company) == {
        "company_code": "1000", "open_items": Decimal("750.00"), "ledger_balance": Decimal("750.00"),
        "difference": Decimal("0.00"), "reconciled": True,
    }


def test_explicit_allocation_and_reversal(demo):
    supplier_invoice(demo)
    supplier_invoice(demo, "1150.00", date(2026, 5, 1))
    newer = finance.open_items(demo.company, reconciliation_type=SUPPLIER)[1]
    entry = pay(demo, "1150.00", allocations=[(newer["line_id"], Decimal("1150.00"))])
    assert open_amounts(demo) == [("KR00000001", Decimal("4600.00"))]
    finance.reverse_journal_entry(entry)
    assert len(open_amounts(demo)) == 2


@pytest.mark.parametrize("amount, allocation, message", [
    ("0", None, "greater than zero"),
    ("9999.00", None, "fully allocated"),
    ("100.00", "too_much", "between 0.01 and its open amount"),
    ("100.00", "customer_item", "is not an open item of supplier"),
])
def test_payment_rules(demo, amount, allocation, message):
    supplier_invoice(demo)
    supplier_line = finance.open_items(demo.company, reconciliation_type=SUPPLIER)[0]["line_id"]
    customer_line = finance.open_items(demo.company)[0]["line_id"]  # Acme's DR00000001
    allocations = {None: None, "too_much": [(supplier_line, Decimal("5000.00"))],
                   "customer_item": [(customer_line, Decimal("100.00"))]}[allocation]
    with pytest.raises(PostingError, match=message):
        pay(demo, amount, allocations)
    assert OpenItemClearing.objects.count() == 0


def test_payment_needs_a_supplier_and_a_bank_account(demo):
    with pytest.raises(PostingError, match="not a supplier"):
        pay(demo, "10.00", supplier=demo.customer)
    with pytest.raises(PostingError, match="cannot be used as the bank account"):
        finance.post_outgoing_payment(company_code=demo.company, supplier=demo.supplier, payment_date=PAY_DAY,
                                      amount=Decimal("10"), bank_account=demo.account("2000"))


def test_payables_ageing(demo):
    supplier_invoice(demo)  # due 2026-05-01
    supplier_invoice(demo, "1150.00", date(2026, 6, 15))  # due 2026-07-15
    report = finance.payables_ageing(demo.company, date(2026, 7, 1))
    [highveld] = report["suppliers"]
    assert (highveld["days_61_90"], highveld["not_due"], highveld["total"]) == (
        Decimal("4600.00"), Decimal("1150.00"), Decimal("5750.00"),
    )
    assert report["totals"]["total"] == Decimal("5750.00")
    assert finance.payables_ageing(demo.company, date(2026, 5, 1))["totals"]["total"] == Decimal("4600.00")


def test_receivables_are_unaffected_by_payables(demo):
    supplier_invoice(demo)
    assert finance.receivables_reconciliation(demo.company)["reconciled"] is True
    assert [i["document_number"] for i in finance.open_items(demo.company)] == ["DR00000001"]


# --- API --------------------------------------------------------------------------------------

def test_outgoing_payment_api(demo, client_for):
    supplier_invoice(demo)
    accountant = client_for(Role.ACCOUNTANT)
    payload = {"company_code": "1000", "supplier": demo.supplier.number, "payment_date": "2026-06-10",
               "amount": "600.00", "bank_account": "1000", "reference": "EFT-77"}
    response = accountant.post("/api/finance/outgoing-payments/", payload, format="json")
    assert response.status_code == 201, response.data
    assert (response.data["document_number"], response.data["cleared_items"][0]["document_number"]) == (
        "KZ00000001", "KR00000001",
    )
    line_id = finance.open_items(demo.company, reconciliation_type=SUPPLIER)[0]["line_id"]
    explicit = {**payload, "amount": "100.00", "allocations": [{"line_id": line_id, "amount": "100.00"}]}
    assert accountant.post("/api/finance/outgoing-payments/", explicit, format="json").status_code == 201
    rule = accountant.post("/api/finance/outgoing-payments/", {**payload, "supplier": demo.customer.number},
                           format="json")
    assert rule.status_code == 400
    assert "not a supplier" in str(rule.data["non_field_errors"])


def test_payables_report_endpoints(demo, client_for):
    supplier_invoice(demo)
    accountant = client_for(Role.ACCOUNTANT)
    items = accountant.get("/api/finance/reports/open-items/", {"company_code": "1000", "type": "supplier"}).data
    assert [i["document_number"] for i in items] == ["KR00000001"]
    assert accountant.get("/api/finance/reports/open-items/",
                          {"company_code": "1000", "type": "vendor"}).status_code == 400
    ageing = accountant.get("/api/finance/reports/payables-ageing/",
                            {"company_code": "1000", "as_of": "2026-07-01"}).data
    assert ageing["totals"]["days_61_90"] == Decimal("4600.00")
    reconciliation = accountant.get("/api/finance/reports/payables-reconciliation/", {"company_code": "1000"}).data
    assert (reconciliation["reconciled"], reconciliation["ledger_balance"]) == (True, Decimal("4600.00"))
