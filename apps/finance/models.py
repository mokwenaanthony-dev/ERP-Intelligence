"""Finance core: chart of accounts, fiscal calendar, tax codes and the Universal Journal.

The Universal Journal follows SAP S/4HANA's ACDOCA idea: ``JournalLine`` is the
single line-item table that receives the financial impact of every module. Each
line carries all its dimensions (company code, period, account, partner,
material, plant, tax code, both currencies) and there are no stored totals, so
any report is an aggregation over this one table.

Posted documents are immutable. Corrections are made with a reversal document.
"""
from django.db import models

from apps.common.models import AuditedModel


class ChartOfAccounts(AuditedModel):
    """A list of G/L accounts shared by one or more company codes."""

    code = models.CharField(max_length=4, unique=True)
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "charts of accounts"

    def __str__(self):
        return f"{self.code} {self.name}"


class GLAccount(AuditedModel):
    """A general ledger account. Its type decides whether it carries forward (balance sheet) or
    is closed to retained earnings at year end (income statement).
    """

    class AccountType(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        EQUITY = "EQUITY", "Equity"
        REVENUE = "REVENUE", "Revenue"
        EXPENSE = "EXPENSE", "Expense"

    class ReconciliationType(models.TextChoices):
        NONE = "NONE", "Not a reconciliation account"
        CUSTOMER = "CUSTOMER", "Customers (accounts receivable)"
        SUPPLIER = "SUPPLIER", "Suppliers (accounts payable)"

    BALANCE_SHEET_TYPES = (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY)

    chart_of_accounts = models.ForeignKey(ChartOfAccounts, on_delete=models.PROTECT, related_name="accounts")
    number = models.CharField(max_length=10)
    name = models.CharField(max_length=200)
    account_type = models.CharField(max_length=10, choices=AccountType.choices)
    reconciliation_type = models.CharField(
        max_length=10, choices=ReconciliationType.choices, default=ReconciliationType.NONE,
        help_text="Reconciliation accounts can only be posted together with a business partner.",
    )
    is_posting_blocked = models.BooleanField(default=False)
    cost_center_required = models.BooleanField(
        default=False, help_text="Postings to this (expense) account must name a cost centre.",
    )

    class Meta:
        ordering = ["chart_of_accounts__code", "number"]
        constraints = [
            models.UniqueConstraint(fields=["chart_of_accounts", "number"], name="unique_gl_account_per_chart"),
        ]

    @property
    def is_balance_sheet(self) -> bool:
        """Assets, liabilities and equity carry their balance into the next year."""
        return self.account_type in self.BALANCE_SHEET_TYPES

    def __str__(self):
        return f"{self.number} {self.name}"


class FiscalYear(AuditedModel):
    """Named after the calendar year it ends in (FY2027 = March 2026 - February 2027)."""

    company_code = models.ForeignKey(
        "organization.CompanyCode", on_delete=models.PROTECT, related_name="fiscal_years"
    )
    year = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ["company_code__code", "-year"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "year"], name="unique_fiscal_year_per_company"),
        ]

    def __str__(self):
        return f"{self.company_code.code} FY{self.year}"


class PostingPeriod(AuditedModel):
    """One month of a fiscal year. Postings are only accepted while the period is open."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.CASCADE, related_name="periods")
    number = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)

    class Meta:
        ordering = ["fiscal_year__company_code__code", "fiscal_year__year", "number"]
        constraints = [
            models.UniqueConstraint(fields=["fiscal_year", "number"], name="unique_period_per_fiscal_year"),
        ]

    @property
    def is_open(self) -> bool:
        """True when postings may be made in this period."""
        return self.status == self.Status.OPEN

    def __str__(self):
        return f"{self.fiscal_year} P{self.number:02d}"


class TaxCode(AuditedModel):
    """A VAT code (input or output) with its rate and the G/L account the tax is posted to."""

    class TaxType(models.TextChoices):
        OUTPUT = "OUTPUT", "Output tax (on sales)"
        INPUT = "INPUT", "Input tax (on purchases)"

    chart_of_accounts = models.ForeignKey(ChartOfAccounts, on_delete=models.PROTECT, related_name="tax_codes")
    code = models.CharField(max_length=4)
    description = models.CharField(max_length=200)
    tax_type = models.CharField(max_length=6, choices=TaxType.choices)
    rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percentage, e.g. 15.00 for VAT.")
    gl_account = models.ForeignKey(
        GLAccount, on_delete=models.PROTECT, null=True, blank=True, related_name="tax_codes",
        help_text="Account the tax amount posts to. Not needed for 0% codes.",
    )

    class Meta:
        ordering = ["chart_of_accounts__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["chart_of_accounts", "code"], name="unique_tax_code_per_chart"),
            models.CheckConstraint(condition=models.Q(rate__gte=0, rate__lte=100), name="tax_rate_between_0_and_100"),
        ]

    def __str__(self):
        return f"{self.code} {self.description} ({self.rate}%)"


class DocumentType(models.TextChoices):
    """SAP-style journal entry types, e.g. SA (G/L), DR (customer invoice), DZ (incoming payment)."""

    GENERAL = "SA", "General journal"
    CUSTOMER_INVOICE = "DR", "Customer invoice"
    CUSTOMER_PAYMENT = "DZ", "Customer payment"
    SUPPLIER_INVOICE = "KR", "Supplier invoice"
    SUPPLIER_PAYMENT = "KZ", "Supplier payment"
    GOODS_RECEIPT = "WE", "Goods receipt"
    GOODS_ISSUE = "WA", "Goods issue"
    INVENTORY_ADJUSTMENT = "WI", "Inventory adjustment"
    PAYROLL = "PY", "Payroll posting"
    PRODUCTION_CONFIRMATION = "PC", "Production confirmation (activity allocation)"
    PRODUCTION_SETTLEMENT = "PS", "Production order settlement"


class NumberRange(models.Model):
    """Next document number per company code, fiscal year and document type.

    Shared by journal entries (``DocumentType`` codes) and material documents ("MD").
    """

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    fiscal_year = models.PositiveSmallIntegerField()
    document_type = models.CharField(max_length=2)
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "fiscal_year", "document_type"], name="unique_number_range"
            ),
        ]

    def __str__(self):
        return f"{self.company_code.code} {self.fiscal_year} {self.document_type}: next {self.next_number}"


class ImmutableDocumentError(Exception):
    """Raised on any attempt to change or delete a posted journal document."""


class ImmutableModel(models.Model):
    """Abstract base for posted documents: insert once, never update or delete."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        """Insert once. Any later save raises; corrections are made by posting a reversal."""
        if not self._state.adding:
            raise ImmutableDocumentError("Posted journal documents cannot be changed; post a reversal instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Posted documents are never deleted; post a reversal instead."""
        raise ImmutableDocumentError("Posted journal documents cannot be deleted; post a reversal instead.")


class JournalEntry(ImmutableModel, AuditedModel):
    """Document header. Created only through ``apps.finance.services.post_journal_entry``."""

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    company_code = models.ForeignKey(
        "organization.CompanyCode", on_delete=models.PROTECT, related_name="journal_entries"
    )
    fiscal_year = models.PositiveSmallIntegerField()
    period = models.PositiveSmallIntegerField()
    document_number = models.CharField(max_length=12)
    document_type = models.CharField(max_length=2, choices=DocumentType.choices)
    document_date = models.DateField()
    posting_date = models.DateField()
    reference = models.CharField(max_length=30, blank=True, help_text="External reference, e.g. invoice number.")
    header_text = models.CharField(max_length=100, blank=True)
    currency = models.ForeignKey("masterdata.Currency", on_delete=models.PROTECT, related_name="+")
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.POSTED)
    reversal_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversed_by"
    )

    class Meta:
        ordering = ["-posting_date", "-id"]
        verbose_name_plural = "journal entries"
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "fiscal_year", "document_number"], name="unique_document_number"
            ),
        ]

    def __str__(self):
        return f"{self.company_code.code}/{self.fiscal_year}/{self.document_number}"


class JournalLine(ImmutableModel):
    """The Universal Journal: one row per line item, every dimension on the row.

    Amounts are signed: debits positive, credits negative, so any balance is a
    plain SUM. ``debit_credit`` repeats the sign as an explicit indicator.
    """

    class Side(models.TextChoices):
        DEBIT = "D", "Debit"
        CREDIT = "C", "Credit"

    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    line_number = models.PositiveSmallIntegerField()

    # Dimensions copied from the header so the journal can be analysed on its own.
    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    fiscal_year = models.PositiveSmallIntegerField()
    period = models.PositiveSmallIntegerField()
    posting_date = models.DateField()

    gl_account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name="journal_lines")
    debit_credit = models.CharField(max_length=1, choices=Side.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2, help_text="Signed, in the document currency.")
    currency = models.ForeignKey("masterdata.Currency", on_delete=models.PROTECT, related_name="+")
    amount_local = models.DecimalField(max_digits=18, decimal_places=2, help_text="Signed, in company currency.")
    local_currency = models.ForeignKey("masterdata.Currency", on_delete=models.PROTECT, related_name="+")

    business_partner = models.ForeignKey(
        "masterdata.BusinessPartner", on_delete=models.PROTECT, null=True, blank=True, related_name="journal_lines"
    )
    material = models.ForeignKey(
        "masterdata.Material", on_delete=models.PROTECT, null=True, blank=True, related_name="journal_lines"
    )
    plant = models.ForeignKey(
        "organization.Plant", on_delete=models.PROTECT, null=True, blank=True, related_name="journal_lines"
    )
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    cost_center = models.ForeignKey(
        "controlling.CostCenter", on_delete=models.PROTECT, null=True, blank=True, related_name="journal_lines"
    )
    profit_center = models.ForeignKey(
        "controlling.ProfitCenter", on_delete=models.PROTECT, null=True, blank=True, related_name="journal_lines",
        help_text="Given, or derived from the cost centre or the plant.",
    )
    order_number = models.CharField(
        max_length=20, blank=True, default="", db_index=True,
        help_text="Production order this line is charged to (SAP AUFNR), a cost object like a cost centre.",
    )
    is_tax_line = models.BooleanField(default=False)
    text = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["entry_id", "line_number"]
        constraints = [
            models.UniqueConstraint(fields=["entry", "line_number"], name="unique_line_number_per_entry"),
            models.CheckConstraint(
                condition=(
                    models.Q(debit_credit="D", amount__gt=0, amount_local__gte=0)
                    | models.Q(debit_credit="C", amount__lt=0, amount_local__lte=0)
                ),
                name="journal_line_sign_matches_side",
            ),
        ]
        indexes = [
            models.Index(fields=["company_code", "fiscal_year", "period"], name="jl_company_period_idx"),
            models.Index(fields=["gl_account", "fiscal_year"], name="jl_account_year_idx"),
            models.Index(fields=["business_partner"], name="jl_partner_idx"),
        ]

    def __str__(self):
        return f"{self.entry} line {self.line_number}: {self.gl_account.number} {self.amount}"


class OpenItemClearing(models.Model):
    """Links a payment (the clearing document) to the receivable or payable line it settles.

    An open item is a journal line on a reconciliation account; its open amount is the line
    amount minus the clearings made by documents that are still posted. Reversing a payment
    therefore re-opens the items it cleared.
    """

    item = models.ForeignKey(JournalLine, on_delete=models.PROTECT, related_name="clearings")
    clearing_entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="clearings_made")
    amount = models.DecimalField(max_digits=18, decimal_places=2, help_text="Amount settled, always positive.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="clearing_amount_positive"),
        ]

    def __str__(self):
        return f"{self.clearing_entry} clears {self.amount} of {self.item}"
