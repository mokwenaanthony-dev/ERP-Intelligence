"""Finance business rules. The only way journal entries are created.

Kept out of views and serializers so the same rules apply to the API, the
admin, management commands and (from Wave 2) postings made by Sales,
Purchasing and Inventory.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import logging

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
    OpenItemClearing,
    PostingPeriod,
    TaxCode,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class PostingError(Exception):
    """The document breaks an accounting rule and was not posted."""


@dataclass
class LineInput:
    """One line of a journal entry to post. Amounts are positive; ``debit_credit`` gives the side."""

    gl_account: GLAccount
    debit_credit: str  # "D" or "C"
    amount: Decimal  # positive, in document currency
    business_partner: BusinessPartner | None = None
    material: object = None
    plant: object = None
    tax_code: TaxCode | None = None
    text: str = ""
    is_tax_line: bool = False
    cost_center: object = None
    profit_center: object = None


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
    logger.info("Created fiscal year %s for %s (%s to %s)", year, company_code.code, start, end)
    return fiscal_year


def find_period(company_code, posting_date: date) -> PostingPeriod:
    """The posting period that contains ``posting_date``, or PostingError if no fiscal year covers it."""
    period = (
        PostingPeriod.objects.select_related("fiscal_year")
        .filter(fiscal_year__company_code=company_code, start_date__lte=posting_date, end_date__gte=posting_date)
        .first()
    )
    if period is None:
        raise PostingError(f"No fiscal year is set up for {posting_date} in company code {company_code.code}.")
    return period


def set_period_status(period: PostingPeriod, status: str, user=None) -> PostingPeriod:
    """Open or close a posting period (the Finance Manager's month-end control)."""
    period.status = status
    period.updated_by = user
    period.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Posting period %s set to %s by %s", period, status, getattr(user, "username", "system"))
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

    cost_center, profit_center = line.cost_center, line.profit_center
    if cost_center is not None:
        if cost_center.company_code_id != company_code.pk:
            raise PostingError(f"Cost centre {cost_center.code} does not belong to {company_code.code}.")
        if not cost_center.is_active:
            raise PostingError(f"Cost centre {cost_center.code} is inactive.")
        if account.account_type != GLAccount.AccountType.EXPENSE:
            raise PostingError(f"Account {account.number} is not an expense account; it cannot take a cost centre.")
        if profit_center is not None and profit_center.pk != cost_center.profit_center_id:
            raise PostingError(
                f"Cost centre {cost_center.code} belongs to profit centre {cost_center.profit_center.code}, "
                f"not {profit_center.code}."
            )
    elif account.cost_center_required:
        raise PostingError(f"Account {account.number} needs a cost centre.")
    if profit_center is not None:
        if profit_center.company_code_id != company_code.pk:
            raise PostingError(f"Profit centre {profit_center.code} does not belong to {company_code.code}.")
        if not profit_center.is_active:
            raise PostingError(f"Profit centre {profit_center.code} is inactive.")


def _derive_profit_centers(lines: list[LineInput]) -> list[LineInput]:
    """Profit centre: as given, else the cost centre's, else the plant's default (SAP-style derivation)."""
    from apps.controlling.models import PlantProfitCenter

    plants = {line.plant.pk for line in lines if line.plant is not None}
    by_plant = {link.plant_id: link.profit_center for link in
                PlantProfitCenter.objects.select_related("profit_center").filter(plant_id__in=plants)} if plants else {}
    result = []
    for line in lines:
        profit_center = line.profit_center
        if profit_center is None and line.cost_center is not None:
            profit_center = line.cost_center.profit_center
        if profit_center is None and line.plant is not None:
            profit_center = by_plant.get(line.plant.pk)
        result.append(LineInput(**{**line.__dict__, "profit_center": profit_center}))
    return result


def calculate_tax(amount: Decimal, tax_code: TaxCode) -> Decimal:
    """Tax on a net amount at the code's rate, rounded half-up to cents."""
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
            cost_center=line.cost_center, profit_center=line.profit_center,
        )
        for index, (line, local_amount) in enumerate(zip(lines, local_amounts), start=1)
    ])
    logger.info("Posted %s %s/%s on %s: %d lines, %s %s%s", entry.document_type, company_code.code,
                entry.document_number, posting_date, len(lines), company_code.currency.code,
                sum((a for a in local_amounts if a > 0), Decimal("0")),
                f" (reverses {reversal_of.document_number})" if reversal_of else "")
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
    lines = _derive_profit_centers(_with_tax_lines(lines))
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
            cost_center=line.cost_center, profit_center=line.profit_center,
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
    logger.info("Reversed %s with %s", entry.document_number, reversal.document_number)
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


# --- Open items, payments and receivables ----------------------------------------------

def _recon_sign(reconciliation_type) -> int:
    """Receivables are debit items (+), payables are credit items (-)."""
    return 1 if reconciliation_type == GLAccount.ReconciliationType.CUSTOMER else -1


def open_items(company_code, *, reconciliation_type=GLAccount.ReconciliationType.CUSTOMER, partner=None,
               include_cleared=False) -> list[dict]:
    """Receivable (or payable) line items with their open amount, oldest first.

    Lines of reversed documents and of reversal documents are not open items: the two cancel out.
    """
    side = {"amount__gt": 0} if _recon_sign(reconciliation_type) > 0 else {"amount__lt": 0}
    lines = (
        JournalLine.objects.select_related("entry", "business_partner", "gl_account")
        .filter(
            company_code=company_code, gl_account__reconciliation_type=reconciliation_type,
            entry__status=JournalEntry.Status.POSTED, entry__reversal_of__isnull=True, **side,
        )
        .order_by("posting_date", "id")
    )
    if partner is not None:
        lines = lines.filter(business_partner=partner)

    cleared = dict(
        OpenItemClearing.objects.filter(
            item__in=lines, clearing_entry__status=JournalEntry.Status.POSTED,
            clearing_entry__reversal_of__isnull=True,
        ).values("item").annotate(total=Sum("amount")).values_list("item", "total")
    )
    terms = _payment_terms(company_code, reconciliation_type)
    result = []
    for line in lines:
        original = abs(line.amount_local)
        cleared_amount = Decimal(cleared.get(line.pk) or 0).quantize(CENT)
        open_amount = original - cleared_amount
        if open_amount <= 0 and not include_cleared:
            continue
        days = terms.get(line.business_partner_id, 30)
        result.append({
            "line_id": line.pk, "document_number": line.entry.document_number,
            "document_type": line.entry.document_type,
            "reference": line.entry.reference, "posting_date": line.posting_date,
            "due_date": line.posting_date + timedelta(days=days),
            "business_partner": line.business_partner.number if line.business_partner else None,
            "business_partner_name": line.business_partner.name if line.business_partner else None,
            "gl_account": line.gl_account.number, "amount": original, "cleared": cleared_amount,
            "open_amount": open_amount,
        })
    return result


def _payment_terms(company_code, reconciliation_type) -> dict:
    from apps.masterdata.models import CustomerRole, SupplierRole

    model = CustomerRole if reconciliation_type == GLAccount.ReconciliationType.CUSTOMER else SupplierRole
    return dict(model.objects.filter(company_code=company_code).values_list("business_partner", "payment_terms_days"))


def _partner_role(company_code, partner, reconciliation_type):
    from apps.masterdata.models import CustomerRole, SupplierRole

    customer = reconciliation_type == GLAccount.ReconciliationType.CUSTOMER
    model = CustomerRole if customer else SupplierRole
    role = model.objects.select_related("reconciliation_account").filter(
        business_partner=partner, company_code=company_code
    ).first()
    if role is None:
        kind = "customer" if customer else "supplier"
        raise PostingError(f"Business partner {partner.number} is not a {kind} of {company_code.code}.")
    return role


@transaction.atomic
def _post_partner_payment(*, reconciliation_type, company_code, partner, payment_date: date, amount: Decimal,
                          bank_account: GLAccount, allocations, reference: str, header_text: str,
                          user) -> JournalEntry:
    """Shared rules for incoming (customer) and outgoing (supplier) payments."""
    customer = reconciliation_type == GLAccount.ReconciliationType.CUSTOMER
    amount = Decimal(amount).quantize(CENT)
    if amount <= 0:
        raise PostingError("The payment amount must be greater than zero.")
    role = _partner_role(company_code, partner, reconciliation_type)
    if bank_account.reconciliation_type != GLAccount.ReconciliationType.NONE:
        raise PostingError(f"Account {bank_account.number} cannot be used as the bank account.")

    # Lock the partner's items so two payments cannot clear the same invoice at once.
    list(JournalLine.objects.select_for_update(of=("self",)).filter(
        company_code=company_code, business_partner=partner, gl_account__reconciliation_type=reconciliation_type
    ).values_list("pk", flat=True))
    items = {item["line_id"]: item for item in open_items(
        company_code, reconciliation_type=reconciliation_type, partner=partner
    )}

    if allocations is None:
        allocations, remaining = [], amount
        for item in items.values():
            if remaining <= 0:
                break
            part = min(remaining, item["open_amount"])
            allocations.append((item["line_id"], part))
            remaining -= part
    allocations = [(line_id, Decimal(part).quantize(CENT)) for line_id, part in allocations]

    kind = "customer" if customer else "supplier"
    for line_id, part in allocations:
        item = items.get(line_id)
        if item is None:
            raise PostingError(f"Item {line_id} is not an open item of {kind} {partner.number}.")
        if part <= 0 or part > item["open_amount"]:
            raise PostingError(
                f"Allocation of {part} to {item['document_number']} must be between 0.01 and its open amount "
                f"{item['open_amount']}."
            )
    allocated = sum((part for _, part in allocations), ZERO)
    if allocated != amount:
        raise PostingError(f"Payment of {amount} must be fully allocated to open items (allocated {allocated}).")

    debit, credit = JournalLine.Side.DEBIT, JournalLine.Side.CREDIT
    if customer:  # money in: Dr bank / Cr receivables
        document_type, text = DocumentType.CUSTOMER_PAYMENT, f"Payment from {partner.name}"
        lines = [LineInput(bank_account, debit, amount, business_partner=partner),
                 LineInput(role.reconciliation_account, credit, amount, business_partner=partner)]
    else:  # money out: Dr payables / Cr bank
        document_type, text = DocumentType.SUPPLIER_PAYMENT, f"Payment to {partner.name}"
        lines = [LineInput(role.reconciliation_account, debit, amount, business_partner=partner),
                 LineInput(bank_account, credit, amount, business_partner=partner)]
    entry = post_journal_entry(
        company_code=company_code, document_type=document_type, posting_date=payment_date,
        reference=reference, header_text=header_text or text[:100], user=user, lines=lines,
    )
    OpenItemClearing.objects.bulk_create([
        OpenItemClearing(item_id=line_id, clearing_entry=entry, amount=part) for line_id, part in allocations
    ])
    logger.info("%s payment %s for partner %s: %s cleared across %d open item(s)",
                "Incoming" if customer else "Outgoing", entry.document_number, partner.number, amount,
                len(allocations))
    return entry


def post_incoming_payment(*, company_code, customer, payment_date: date, amount: Decimal, bank_account: GLAccount,
                          allocations: list[tuple[int, Decimal]] | None = None, reference: str = "",
                          header_text: str = "", user=None) -> JournalEntry:
    """Post a customer payment (Dr bank / Cr receivables) and clear the open items it pays.

    ``allocations`` is a list of (open item line id, amount). Without it, the payment clears the
    customer's oldest items first. The whole amount must be allocated.
    """
    return _post_partner_payment(
        reconciliation_type=GLAccount.ReconciliationType.CUSTOMER, company_code=company_code, partner=customer,
        payment_date=payment_date, amount=amount, bank_account=bank_account, allocations=allocations,
        reference=reference, header_text=header_text, user=user,
    )


def post_outgoing_payment(*, company_code, supplier, payment_date: date, amount: Decimal, bank_account: GLAccount,
                          allocations: list[tuple[int, Decimal]] | None = None, reference: str = "",
                          header_text: str = "", user=None) -> JournalEntry:
    """Pay a supplier (Dr payables / Cr bank) and clear the supplier invoices it pays. Same rules as
    ``post_incoming_payment``: oldest first without allocations, and the whole amount must be allocated."""
    return _post_partner_payment(
        reconciliation_type=GLAccount.ReconciliationType.SUPPLIER, company_code=company_code, partner=supplier,
        payment_date=payment_date, amount=amount, bank_account=bank_account, allocations=allocations,
        reference=reference, header_text=header_text, user=user,
    )


AGEING_BUCKETS = ("not_due", "days_1_30", "days_31_60", "days_61_90", "over_90")


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "not_due"
    if days_overdue <= 30:
        return "days_1_30"
    if days_overdue <= 60:
        return "days_31_60"
    if days_overdue <= 90:
        return "days_61_90"
    return "over_90"


def _ageing(company_code, as_of: date, reconciliation_type, key: str) -> dict:
    partners = {}
    totals = dict.fromkeys(AGEING_BUCKETS, ZERO)
    for item in open_items(company_code, reconciliation_type=reconciliation_type):
        if item["posting_date"] > as_of:
            continue
        bucket = _bucket((as_of - item["due_date"]).days)
        row = partners.setdefault(item["business_partner"], {
            "business_partner": item["business_partner"], "name": item["business_partner_name"],
            **dict.fromkeys(AGEING_BUCKETS, ZERO), "total": ZERO,
        })
        row[bucket] += item["open_amount"]
        row["total"] += item["open_amount"]
        totals[bucket] += item["open_amount"]
    return {
        "company_code": company_code.code, "currency": company_code.currency.code, "as_of": as_of,
        key: sorted(partners.values(), key=lambda r: r["business_partner"] or ""),
        "totals": {**totals, "total": sum(totals.values(), ZERO)},
    }


def receivables_ageing(company_code, as_of: date) -> dict:
    """Open receivables per customer, split by how many days past the due date they are."""
    return _ageing(company_code, as_of, GLAccount.ReconciliationType.CUSTOMER, "customers")


def payables_ageing(company_code, as_of: date) -> dict:
    """Open payables per supplier, split by how many days past the due date they are."""
    return _ageing(company_code, as_of, GLAccount.ReconciliationType.SUPPLIER, "suppliers")


def _reconciliation(company_code, reconciliation_type) -> dict:
    open_total = sum((item["open_amount"] for item in open_items(
        company_code, reconciliation_type=reconciliation_type
    )), ZERO)
    ledger = JournalLine.objects.filter(
        company_code=company_code, gl_account__reconciliation_type=reconciliation_type
    ).aggregate(total=Sum("amount_local"))["total"]
    ledger = _money(ledger) * _recon_sign(reconciliation_type)
    return {"company_code": company_code.code, "open_items": open_total, "ledger_balance": ledger,
            "difference": open_total - ledger, "reconciled": open_total == ledger}


def receivables_reconciliation(company_code) -> dict:
    """Sum of open receivable items versus the balance of the receivables reconciliation accounts."""
    return _reconciliation(company_code, GLAccount.ReconciliationType.CUSTOMER)


def payables_reconciliation(company_code) -> dict:
    """Sum of open payable items versus the (credit) balance of the payables reconciliation accounts,
    shown as a positive amount."""
    return _reconciliation(company_code, GLAccount.ReconciliationType.SUPPLIER)
