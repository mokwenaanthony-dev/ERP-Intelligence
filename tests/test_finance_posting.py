"""Accounting rules of the posting service (apps.finance.services)."""
from datetime import date
from decimal import Decimal

import pytest

from apps.finance import services
from apps.finance.models import (
    ChartOfAccounts,
    GLAccount,
    ImmutableDocumentError,
    JournalEntry,
    JournalLine,
    PostingPeriod,
)
from apps.finance.services import LineInput, PostingError
from apps.masterdata.models import BusinessPartner, Currency, ExchangeRate

pytestmark = pytest.mark.django_db

D, C = "D", "C"
MAY_15 = date(2026, 5, 15)  # fiscal year 2027, period 3


def post(demo, *lines, **kwargs):
    kwargs.setdefault("posting_date", MAY_15)
    return services.post_journal_entry(company_code=demo.company, lines=list(lines), **kwargs)


def rent_entry(demo, amount="1000.00", **kwargs):
    return post(
        demo,
        LineInput(demo.account("6100"), D, Decimal(amount)),
        LineInput(demo.account("1000"), C, Decimal(amount)),
        **kwargs,
    )


# --- Fiscal calendar ------------------------------------------------------------------

@pytest.mark.parametrize(
    "start_month, year, start, end",
    [
        (3, 2027, date(2026, 3, 1), date(2027, 2, 28)),
        (1, 2026, date(2026, 1, 1), date(2026, 12, 31)),
        (3, 2028, date(2027, 3, 1), date(2028, 2, 29)),  # leap year
        (7, 2027, date(2026, 7, 1), date(2027, 6, 30)),
    ],
)
def test_fiscal_year_bounds(start_month, year, start, end):
    assert services.fiscal_year_bounds(start_month, year) == (start, end)


def test_fiscal_year_has_12_open_monthly_periods(demo):
    periods = list(PostingPeriod.objects.filter(fiscal_year__company_code=demo.company, fiscal_year__year=2027))
    assert [p.number for p in periods] == list(range(1, 13))
    assert periods[0].start_date == date(2026, 3, 1)
    assert periods[10].end_date == date(2027, 1, 31)
    assert periods[11].end_date == date(2027, 2, 28)
    assert all(p.is_open for p in periods)


def test_duplicate_fiscal_year_rejected(demo):
    with pytest.raises(PostingError, match="already exists"):
        services.create_fiscal_year(demo.company, 2027)


# --- Posting rules --------------------------------------------------------------------

def test_balanced_entry_is_posted_with_number_and_period(demo, make_user):
    user = make_user(username="poster")
    entry = rent_entry(demo, user=user, reference="LEASE-05", header_text="May rent")

    assert entry.document_number == "SA00000002"  # SA00000001 is the demo opening entry
    assert (entry.fiscal_year, entry.period, entry.status) == (2027, 3, "POSTED")
    assert entry.created_by == user
    lines = list(entry.lines.order_by("line_number"))
    assert [(line.gl_account.number, line.debit_credit, line.amount) for line in lines] == [
        ("6100", "D", Decimal("1000.00")),
        ("1000", "C", Decimal("-1000.00")),
    ]
    assert all(line.amount_local == line.amount for line in lines)
    assert all((line.company_code, line.fiscal_year, line.period) == (demo.company, 2027, 3) for line in lines)


def test_unbalanced_entry_is_rejected_and_nothing_is_saved(demo):
    before = JournalEntry.objects.count()
    with pytest.raises(PostingError, match="do not balance"):
        post(
            demo, LineInput(demo.account("6100"), D, Decimal("100")), LineInput(demo.account("1000"), C, Decimal("90"))
        )
    assert JournalEntry.objects.count() == before


def test_single_line_rejected(demo):
    with pytest.raises(PostingError, match="at least two lines"):
        post(demo, LineInput(demo.account("6100"), D, Decimal("100")))


@pytest.mark.parametrize("amount", ["0", "-5"])
def test_non_positive_amount_rejected(demo, amount):
    with pytest.raises(PostingError, match="greater than zero"):
        rent_entry(demo, amount=amount)


def test_invalid_side_rejected(demo):
    with pytest.raises(PostingError, match="debit"):
        post(demo, LineInput(demo.account("6100"), "X", Decimal("1")), LineInput(demo.account("1000"), C, Decimal("1")))


def test_account_from_another_chart_rejected(demo):
    other = ChartOfAccounts.objects.create(code="OTHR", name="Other")
    foreign = GLAccount.objects.create(chart_of_accounts=other, number="6100", name="Rent", account_type="EXPENSE")
    with pytest.raises(PostingError, match="not in the chart"):
        post(demo, LineInput(foreign, D, Decimal("1")), LineInput(demo.account("1000"), C, Decimal("1")))


def test_blocked_account_rejected(demo):
    GLAccount.objects.filter(pk=demo.account("6100").pk).update(is_posting_blocked=True)
    with pytest.raises(PostingError, match="blocked for posting"):
        rent_entry(demo)


def test_reconciliation_account_needs_partner(demo):
    with pytest.raises(PostingError, match="needs a business partner"):
        post(demo, LineInput(demo.account("1100"), D, Decimal("10")), LineInput(demo.account("4000"), C, Decimal("10")))


def test_partner_without_matching_role_rejected(demo):
    # The supplier has no customer role, so it cannot post to receivables.
    with pytest.raises(PostingError, match="no customer role"):
        post(
            demo,
            LineInput(demo.account("1100"), D, Decimal("10"), business_partner=demo.supplier),
            LineInput(demo.account("4000"), C, Decimal("10")),
        )


def test_blocked_partner_rejected(demo):
    BusinessPartner.objects.filter(pk=demo.customer.pk).update(is_blocked=True)
    demo.customer.refresh_from_db()
    with pytest.raises(PostingError, match="is blocked"):
        post(
            demo,
            LineInput(demo.account("1100"), D, Decimal("10"), business_partner=demo.customer),
            LineInput(demo.account("4000"), C, Decimal("10")),
        )


def test_supplier_invoice_with_input_vat(demo):
    entry = post(
        demo,
        LineInput(demo.account("6200"), D, Decimal("2000.00"), tax_code=demo.tax("I1")),
        LineInput(demo.account("2000"), C, Decimal("2300.00"), business_partner=demo.supplier),
        document_type="KR",
    )
    lines = {line.gl_account.number: line for line in entry.lines.all()}
    assert lines["1300"].amount == Decimal("300.00")  # input VAT added automatically
    assert lines["1300"].is_tax_line and lines["1300"].tax_code.code == "I1"
    assert entry.document_number == "KR00000001"


def test_output_vat_line_is_added_automatically(demo):
    entry = post(
        demo,
        LineInput(demo.account("1100"), D, Decimal("1150.00"), business_partner=demo.customer),
        LineInput(demo.account("4000"), C, Decimal("1000.00"), tax_code=demo.tax("O1")),
        document_type="DR",
    )
    tax_line = entry.lines.get(is_tax_line=True)
    assert (tax_line.gl_account.number, tax_line.amount, tax_line.line_number) == ("2100", Decimal("-150.00"), 3)


def test_vat_is_rounded_half_up(demo):
    assert services.calculate_tax(Decimal("99.99"), demo.tax("O1")) == Decimal("15.00")
    assert services.calculate_tax(Decimal("0.03"), demo.tax("O1")) == Decimal("0.00")


def test_zero_rated_code_adds_no_tax_line(demo):
    entry = post(
        demo,
        LineInput(demo.account("1100"), D, Decimal("500"), business_partner=demo.customer),
        LineInput(demo.account("4000"), C, Decimal("500"), tax_code=demo.tax("O0")),
    )
    assert entry.lines.count() == 2


def test_tax_code_without_account_rejected(demo):
    tax = demo.tax("O1")
    tax.gl_account = None
    with pytest.raises(PostingError, match="no tax account"):
        services._with_tax_lines([LineInput(demo.account("4000"), C, Decimal("10"), tax_code=tax)])


def test_tax_code_from_another_chart_rejected(demo):
    from apps.finance.models import TaxCode

    other = ChartOfAccounts.objects.create(code="OTHR", name="Other")
    foreign_tax = TaxCode.objects.create(chart_of_accounts=other, code="O1", description="x", tax_type="OUTPUT",
                                         rate=Decimal("0"))
    with pytest.raises(PostingError, match="does not belong"):
        post(demo, LineInput(demo.account("6100"), D, Decimal("1"), tax_code=foreign_tax),
             LineInput(demo.account("1000"), C, Decimal("1")))


# --- Currencies -----------------------------------------------------------------------

def test_foreign_currency_is_converted_to_company_currency(demo):
    entry = rent_entry(demo, amount="100.00", currency=demo.currency("USD"))  # demo rate: 1 USD = 18 ZAR
    assert entry.exchange_rate == Decimal("18.000000")
    line = entry.lines.get(gl_account__number="6100")
    assert (line.amount, line.currency.code, line.amount_local, line.local_currency.code) == (
        Decimal("100.00"), "USD", Decimal("1800.00"), "ZAR",
    )


def test_conversion_rounding_difference_keeps_local_currency_balanced(demo):
    ExchangeRate.objects.create(
        from_currency=demo.currency("USD"), to_currency=demo.currency("ZAR"),
        rate=Decimal("18.333333"), valid_from=date(2026, 5, 1),
    )
    entry = post(
        demo,
        LineInput(demo.account("6100"), D, Decimal("10.01")),
        LineInput(demo.account("6200"), D, Decimal("10.01")),
        LineInput(demo.account("1000"), C, Decimal("20.02")),
        currency=demo.currency("USD"),
    )
    local = {line.gl_account.number: line.amount_local for line in entry.lines.all()}
    assert local == {"6100": Decimal("183.52"), "6200": Decimal("183.52"), "1000": Decimal("-367.04")}
    assert sum(local.values()) == 0


def test_missing_exchange_rate_rejected(demo):
    yen = Currency.objects.create(code="JPY", name="Yen")
    with pytest.raises(PostingError, match="No exchange rate"):
        rent_entry(demo, currency=yen)


def test_inverse_exchange_rate_is_used_when_needed(demo):
    rate = ExchangeRate.get_rate(demo.currency("ZAR"), demo.currency("USD"), MAY_15)
    assert rate == Decimal("0.055556")


def test_latest_rate_on_or_before_the_date_wins(demo):
    ExchangeRate.objects.create(from_currency=demo.currency("USD"), to_currency=demo.currency("ZAR"),
                                rate=Decimal("17.5"), valid_from=date(2026, 6, 1))
    usd, zar = demo.currency("USD"), demo.currency("ZAR")
    assert ExchangeRate.get_rate(usd, zar, date(2026, 5, 31)) == Decimal("18")
    assert ExchangeRate.get_rate(usd, zar, date(2026, 6, 1)) == Decimal("17.5")


# --- Periods ------------------------------------------------------------------------

def test_date_outside_any_fiscal_year_rejected(demo):
    with pytest.raises(PostingError, match="No fiscal year"):
        rent_entry(demo, posting_date=date(2030, 1, 1))


def test_closed_period_blocks_posting_until_reopened(demo):
    period = services.find_period(demo.company, MAY_15)
    services.set_period_status(period, PostingPeriod.Status.CLOSED)
    with pytest.raises(PostingError, match="is closed"):
        rent_entry(demo)
    services.set_period_status(period, PostingPeriod.Status.OPEN)
    assert rent_entry(demo).period == 3


# --- Numbering ----------------------------------------------------------------------

def test_numbers_are_sequential_per_type_and_restart_each_fiscal_year(demo):
    assert [rent_entry(demo).document_number for _ in range(3)] == ["SA00000002", "SA00000003", "SA00000004"]
    services.create_fiscal_year(demo.company, 2028)
    assert rent_entry(demo, posting_date=date(2027, 4, 1)).document_number == "SA00000001"


# --- Immutability and reversal ------------------------------------------------------

def test_posted_documents_cannot_be_changed_or_deleted(demo):
    entry = rent_entry(demo)
    line = entry.lines.first()
    entry.header_text = "edited"
    for action in (entry.save, entry.delete, line.save, line.delete):
        with pytest.raises(ImmutableDocumentError):
            action()


def test_reversal_mirrors_the_original_and_marks_it_reversed(demo, make_user):
    user = make_user(username="reverser")
    original = rent_entry(demo, amount="250.00")
    reversal = services.reverse_journal_entry(original, user=user)

    original.refresh_from_db()
    assert original.status == "REVERSED"
    assert reversal.reversal_of == original
    assert reversal.header_text == f"Reversal of {original.document_number}"
    assert reversal.created_by == user
    assert [(line.gl_account.number, line.debit_credit, line.amount) for line in reversal.lines.all()] == [
        ("6100", "C", Decimal("-250.00")),
        ("1000", "D", Decimal("250.00")),
    ]
    totals = services.trial_balance(demo.company, 2027, 3, 3)
    assert all(account["balance"] == 0 for account in totals["accounts"])


def test_reversal_copies_tax_lines_without_recalculating(demo):
    invoice = JournalEntry.objects.get(document_number="DR00000001")
    reversal = services.reverse_journal_entry(invoice, posting_date=MAY_15)
    assert reversal.lines.count() == 3
    assert reversal.lines.get(is_tax_line=True).amount == Decimal("1500.00")
    assert reversal.period == 3


def test_entry_cannot_be_reversed_twice(demo):
    original = rent_entry(demo)
    reversal = services.reverse_journal_entry(original)
    with pytest.raises(PostingError, match="already been reversed"):
        services.reverse_journal_entry(original)
    with pytest.raises(PostingError, match="itself a reversal"):
        services.reverse_journal_entry(reversal)


def test_reversal_into_closed_period_fails_and_leaves_original_posted(demo):
    original = rent_entry(demo)
    services.set_period_status(services.find_period(demo.company, MAY_15), PostingPeriod.Status.CLOSED)
    with pytest.raises(PostingError, match="is closed"):
        services.reverse_journal_entry(original)
    original.refresh_from_db()
    assert original.status == "POSTED"


# --- Trial balance ------------------------------------------------------------------

def test_trial_balance_from_demo_postings(demo):
    report = services.trial_balance(demo.company, 2027)
    assert report["balanced"] is True
    assert report["total_debit"] == report["total_credit"] == Decimal("511500.00")
    by_account = {row["account"]: row for row in report["accounts"]}
    assert by_account["2100"]["credit"] == Decimal("1500.00")
    assert by_account["2100"]["balance"] == Decimal("-1500.00")
    assert by_account["1000"]["debit"] == Decimal("500000.00")


def test_trial_balance_respects_period_range(demo):
    rent_entry(demo)  # period 3
    assert "6100" not in {row["account"] for row in services.trial_balance(demo.company, 2027, 1, 2)["accounts"]}
    assert "6100" in {row["account"] for row in services.trial_balance(demo.company, 2027, 3, 3)["accounts"]}


def test_journal_line_string(demo):
    line = JournalLine.objects.filter(entry__document_number="SA00000001").first()
    assert str(line) == "1000/2027/SA00000001 line 1: 1000 500000.00"
