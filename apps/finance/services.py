"""Finance business rules. The only way journal entries are created.

Kept out of views and serializers so the same rules apply to the API, the
admin, management commands and (from Wave 2) postings made by Sales,
Purchasing and Inventory.
"""
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Case, DecimalField, F, Sum, Value, When

from apps.masterdata.models import BusinessPartner, ExchangeRate, ExchangeRateNotFound

from .models import (
    DocumentType,
    FiscalYear,
    GLAccount,
    JournalEntry,
    JournalLine,
    NumberRange,
    PostingPeriod,
    TaxCode,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class PostingError(Exception):
    """The document breaks an accounting rule and was not posted."""


@dataclass
class LineInput:
    gl_account: GLAccount
    debit_credit: str  # "D" or "C"
    amount: Decimal  # positive, in document currency
    business_partner: BusinessPartner | None = None
    material: object = None
    plant: object = None
    tax_code: TaxCode | None = None
    text: str = ""
    is_tax_line: bool = False


# --- Fiscal calendar ------------------------------------------------------------------

def fiscal_year_bounds(start_month: int, year: int) -> tuple[date, date]:
    """Start and end of fiscal ``year``, named after the calendar year it ends in."""
    start_year = year if start_month == 1 else year - 1
    start = date(start_year, start_month, 1)
    end_month = 12 if start_month == 1 else start_month - 1
    end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return start, end


@transaction.atomic
def create_fiscal_year(company_code, year: int, user=None) -> FiscalYear:
    """Create a fiscal year with 12 monthly posting periods, all open."""
    if FiscalYear.objects.filter(company_code=company_code, year=year).exists():
        raise PostingError(f"Fiscal year {year} already exists for company code {company_code.code}.")
    start, end = fiscal_year_bounds(company_code.fiscal_year_start_month, year)
    fiscal_year = FiscalYear.objects.create(
        company_code=company_code, year=year, start_date=start, end_date=end, created_by=user, updated_by=user
    )
    month, cal_year = start.month, start.year
    for number in range(1, 13):
        last_day = calendar.monthrange(cal_year, month)[1]
        PostingPeriod.objects.create(
            fiscal_year=fiscal_year, number=number,
            start_date=date(cal_year, month, 1), end_date=date(cal_year, month, last_day),
            created_by=user, updated_by=user,
        )
        month += 1
        if month == 13:
            month, cal_year = 1, cal_year + 1
    return fiscal_year


def find_period(company_code, posting_date: date) -> PostingPeriod:
    period = (
        PostingPeriod.objects.select_related("fiscal_year")
        .filter(fiscal_year__company_code=company_code, start_date__lte=posting_date, end_date__gte=posting_date)
        .first()
    )
    if period is None:
        raise PostingError(f"No fiscal year is set up for {posting_date} in company code {company_code.code}.")
    return period


def set_period_status(period: PostingPeriod, status: str, user=None) -> PostingPeriod:
    period.status = status
    period.updated_by = user
    period.save(update_fields=["status", "updated_by", "updated_at"])
    return period


# --- Numbering -----------------------------------------------------------------------

def next_document_number(company_code, fiscal_year: int, document_type: str) -> str:
    """Gap-free sequential number per company code, fiscal year and document type.

    Must be called inside a transaction: the row lock serialises concurrent postings.
    """
    number_range, _ = NumberRange.objects.select_for_update().get_or_create(
        company_code=company_code, fiscal_year=fiscal_year, document_type=document_type
    )
    number = number_range.next_number
    number_range.next_number = F("next_number") + 1
    number_range.save(update_fields=["next_number"])
    return f"{document_type}{number:08d}"


# --- Posting -------------------------------------------------------------------------

def _check_line(company_code, line: LineInput) -> None:
    account = line.gl_account
    if account.chart_of_accounts_id != company_code.chart_of_accounts_id:
        raise PostingError(f"Account {account.number} is not in the chart of accounts of {company_code.code}.")
    if account.is_posting_blocked:
        raise PostingError(f"Account {account.number} is blocked for posting.")
    if line.debit_credit not in (JournalLine.Side.DEBIT, JournalLine.Side.CREDIT):
        raise PostingError("Each line must be a debit (D) or a credit (C).")
    if line.amount is None or line.amount <= 0:
        raise PostingError("Line amounts must be greater than zero; use debit or credit for the direction.")

    partner = line.business_partner
    if partner is not None and partner.is_blocked:
        raise PostingError(f"Business partner {partner.number} is blocked.")
    recon = account.reconciliation_type
    if recon != GLAccount.ReconciliationType.NONE:
        if partner is None:
            raise PostingError(f"Account {account.number} is a reconciliation account and needs a business partner.")
        roles = partner.customer_roles if recon == GLAccount.ReconciliationType.CUSTOMER else partner.supplier_roles
        if not roles.filter(company_code=company_code, reconciliation_account=account).exists():
            kind = recon.lower()
            raise PostingError(
                f"Business partner {partner.number} has no {kind} role in {company_code.code} "
                f"using account {account.number}."
            )
    if line.tax_code is not None and line.tax_code.chart_of_accounts_id != company_code.chart_of_accounts_id:
        raise PostingError(f"Tax code {line.tax_code.code} does not belong to {company_code.code}.")


def calculate_tax(amount: Decimal, tax_code: TaxCode) -> Decimal:
    return (amount * tax_code.rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)


def _with_tax_lines(lines: list[LineInput]) -> list[LineInput]:
    """Add a tax line after every line that carries a tax code with a non-zero rate."""
    result = []
    for line in lines:
        result.append(line)
        tax_code = line.tax_code
        if tax_code is None or line.is_tax_line or tax_code.rate == 0:
            continue
        if tax_code.gl_account is None:
            raise PostingError(f"Tax code {tax_code.code} has no tax account.")
        result.append(
            LineInput(
                gl_account=tax_code.gl_account, debit_credit=line.debit_credit,
                amount=calculate_tax(line.amount, tax_code), business_partner=line.business_partner,
                tax_code=tax_code, text=f"VAT {tax_code.code} {tax_code.rate}%", is_tax_line=True,
            )
        )
    return result


def _signed(line: LineInput) -> Decimal:
    return line.amount if line.debit_credit == JournalLine.Side.DEBIT else -line.amount


def _create_entry(*, company_code, document_type, posting_date, document_date, currency, exchange_rate,
                  lines, reference, header_text, user, reversal_of=None) -> JournalEntry:
    period = find_period(company_code, posting_date)
    if not period.is_open:
        raise PostingError(f"Posting period {period} is closed.")

    if len(lines) < 2:
        raise PostingError("A journal entry needs at least two lines.")
    balance = sum((_signed(line) for line in lines), ZERO)
    if balance != 0:
        raise PostingError(f"Debits and credits do not balance (difference {balance} {currency.code}).")

    local_amounts = [(_signed(line) * exchange_rate).quantize(CENT, rounding=ROUND_HALF_UP) for line in lines]
    rounding_difference = sum(local_amounts, ZERO)
    if rounding_difference:
        # Currency conversion rounding: absorb the cents on the largest line.
        largest = max(range(len(local_amounts)), key=lambda i: abs(local_amounts[i]))
        local_amounts[largest] -= rounding_difference

    fiscal_year = period.fiscal_year.year
    entry = JournalEntry.objects.create(
        company_code=company_code, fiscal_year=fiscal_year, period=period.number,
        document_number=next_document_number(company_code, fiscal_year, document_type),
        document_type=document_type, document_date=document_date or posting_date, posting_date=posting_date,
        reference=reference, header_text=header_text, currency=currency, exchange_rate=exchange_rate,
        reversal_of=reversal_of, created_by=user, updated_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(
            entry=entry, line_number=index, company_code=company_code, fiscal_year=fiscal_year,
            period=period.number, posting_date=posting_date, gl_account=line.gl_account,
            debit_credit=line.debit_credit, amount=_signed(line), currency=currency,
            amount_local=local_amount, local_currency=company_code.currency,
            business_partner=line.business_partner, material=line.material, plant=line.plant,
            tax_code=line.tax_code, is_tax_line=line.is_tax_line, text=line.text,
        )
        for index, (line, local_amount) in enumerate(zip(lines, local_amounts), start=1)
    ])
    return entry


@transaction.atomic
def post_journal_entry(*, company_code, document_type=DocumentType.GENERAL, posting_date: date,
                       lines: list[LineInput], currency=None, document_date: date | None = None,
                       reference: str = "", header_text: str = "", user=None) -> JournalEntry:
    """Validate and post a journal entry. Raises ``PostingError`` and posts nothing if any rule fails.

    Lines with a tax code get their tax line added automatically.
    """
    currency = currency or company_code.currency
    for line in lines:
        _check_line(company_code, line)
    lines = [LineInput(**{**line.__dict__, "amount": line.amount.quantize(CENT)}) for line in lines]
    lines = _with_tax_lines(lines)
    try:
        exchange_rate = ExchangeRate.get_rate(currency, company_code.currency, posting_date)
    except ExchangeRateNotFound as exc:
        raise PostingError(str(exc)) from exc
    return _create_entry(
        company_code=company_code, document_type=document_type, posting_date=posting_date,
        document_date=document_date, currency=currency, exchange_rate=exchange_rate, lines=lines,
        reference=reference, header_text=header_text, user=user,
    )


@transaction.atomic
def reverse_journal_entry(entry: JournalEntry, *, posting_date: date | None = None, user=None) -> JournalEntry:
    """Post the mirror image of ``entry`` and mark it reversed. The original stays untouched otherwise."""
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status == JournalEntry.Status.REVERSED:
        raise PostingError(f"Document {entry} has already been reversed.")
    if entry.reversal_of_id is not None:
        raise PostingError(f"Document {entry} is itself a reversal and cannot be reversed.")

    flip = {JournalLine.Side.DEBIT: JournalLine.Side.CREDIT, JournalLine.Side.CREDIT: JournalLine.Side.DEBIT}
    mirror = [
        LineInput(
            gl_account=line.gl_account,
            debit_credit=flip[line.debit_credit],
            amount=abs(line.amount), business_partner=line.business_partner, material=line.material,
            plant=line.plant, tax_code=line.tax_code, text=line.text, is_tax_line=line.is_tax_line,
        )
        for line in entry.lines.select_related("gl_account").order_by("line_number")
    ]
    reversal = _create_entry(
        company_code=entry.company_code, document_type=entry.document_type,
        posting_date=posting_date or entry.posting_date, document_date=posting_date or entry.posting_date,
        currency=entry.currency, exchange_rate=entry.exchange_rate, lines=mirror,
        reference=entry.reference, header_text=f"Reversal of {entry.document_number}"[:100],
        user=user, reversal_of=entry,
    )
    JournalEntry.objects.filter(pk=entry.pk).update(status=JournalEntry.Status.REVERSED, updated_by=user)
    return reversal


# --- Reporting -----------------------------------------------------------------------

def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def trial_balance(company_code, fiscal_year: int, period_from: int = 1, period_to: int = 12) -> dict:
    """Debit, credit and balance per account in company currency, straight from the journal."""
    money = DecimalField(max_digits=18, decimal_places=2)
    rows = (
        JournalLine.objects.filter(
            company_code=company_code, fiscal_year=fiscal_year, period__gte=period_from, period__lte=period_to
        )
        .values("gl_account__number", "gl_account__name", "gl_account__account_type")
        .annotate(
            debit=Sum(Case(When(amount_local__gt=0, then=F("amount_local")), default=Value(ZERO), output_field=money)),
            credit=Sum(
                Case(When(amount_local__lt=0, then=-F("amount_local")), default=Value(ZERO), output_field=money)
            ),
            balance=Sum("amount_local"),
        )
        .order_by("gl_account__number")
    )
    accounts = [
        {
            "account": row["gl_account__number"], "name": row["gl_account__name"],
            "account_type": row["gl_account__account_type"],
            "debit": _money(row["debit"]), "credit": _money(row["credit"]), "balance": _money(row["balance"]),
        }
        for row in rows
    ]
    total_debit = sum((a["debit"] for a in accounts), ZERO)
    total_credit = sum((a["credit"] for a in accounts), ZERO)
    return {
        "company_code": company_code.code, "currency": company_code.currency.code, "fiscal_year": fiscal_year,
        "period_from": period_from, "period_to": period_to, "accounts": accounts,
        "total_debit": total_debit, "total_credit": total_credit, "balanced": total_debit == total_credit,
    }
