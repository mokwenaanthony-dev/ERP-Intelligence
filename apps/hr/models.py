"""Human resources and payroll (South Africa).

* ``OrgUnit`` / ``Position`` - the organisation chart. An org unit carries the cost centre its
  people's salaries are charged to; a position can override it.
* ``Employee`` - personnel master record with hire/termination dates and a monthly salary.
* ``PayeTable`` / ``PayeBracket`` - SARS income tax tables per year of assessment (March-February).
* ``PayrollSetting`` - UIF and SDL rates and the accounts payroll posts to, per company code.
* ``PayrollRun`` / ``PayrollResult`` - one posted payroll per company code and month, one result
  per employee. The run posts a single journal entry: salary costs per cost centre, the PAYE,
  UIF and SDL liabilities and the net pay owed to employees.

Payroll runs are created through ``apps.hr.services``.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.common.models import AuditedModel

MONEY = {"max_digits": 18, "decimal_places": 2}


class OrgUnit(AuditedModel):
    """A department or team in the organisation tree, optionally with its own cost centre."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    cost_center = models.ForeignKey(
        "controlling.CostCenter", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Where the salaries of people in this unit are charged.",
    )

    class Meta:
        ordering = ["company_code__code", "code"]
        verbose_name = "organisational unit"
        constraints = [
            models.UniqueConstraint(fields=["company_code", "code"], name="unique_org_unit_per_company"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class Position(AuditedModel):
    """A job slot in an org unit. Payroll cost goes to its cost centre, or the org unit's."""

    org_unit = models.ForeignKey(OrgUnit, on_delete=models.PROTECT, related_name="positions")
    code = models.CharField(max_length=10)
    title = models.CharField(max_length=100)
    cost_center = models.ForeignKey(
        "controlling.CostCenter", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Overrides the org unit's cost centre.",
    )

    class Meta:
        ordering = ["org_unit__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["org_unit", "code"], name="unique_position_per_org_unit"),
        ]

    @property
    def effective_cost_center(self):
        """The position's own cost centre, otherwise the org unit's (None if neither is set)."""
        return self.cost_center or self.org_unit.cost_center

    def __str__(self):
        return f"{self.code} {self.title}"


class Employee(AuditedModel):
    """A person employed by a company code, holding one position. Pay data is HR-only."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    personnel_number = models.CharField(max_length=12, editable=False)
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    email = models.EmailField(blank=True)
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="employees")
    hire_date = models.DateField()
    termination_date = models.DateField(null=True, blank=True, help_text="Last day of employment.")
    monthly_salary = models.DecimalField(**MONEY, help_text="Gross basic salary per month.")
    tax_reference = models.CharField(max_length=20, blank=True, help_text="SARS income tax reference number.")

    class Meta:
        ordering = ["company_code__code", "personnel_number"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "personnel_number"], name="unique_personnel_number"),
            models.CheckConstraint(condition=Q(monthly_salary__gte=0), name="salary_non_negative"),
            models.CheckConstraint(
                condition=Q(termination_date__isnull=True) | Q(termination_date__gte=models.F("hire_date")),
                name="termination_after_hire",
            ),
        ]

    @property
    def full_name(self) -> str:
        """First and last name, for screens only (never logged or projected with pay data)."""
        return f"{self.first_name} {self.last_name}"

    def __str__(self):
        return f"{self.personnel_number} {self.full_name}"


class PayeTable(AuditedModel):
    """SARS rates of tax for individuals for one year of assessment (e.g. 2027 = 1 Mar 2026 - 28 Feb 2027)."""

    tax_year = models.PositiveSmallIntegerField(unique=True)
    primary_rebate = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["-tax_year"]
        verbose_name = "PAYE table"

    def __str__(self):
        return f"Tax year {self.tax_year} (rebate {self.primary_rebate})"


class PayeBracket(models.Model):
    """Tax on annual income above ``lower_limit`` = ``base_tax`` + ``rate``% of the amount above it."""

    table = models.ForeignKey(PayeTable, on_delete=models.CASCADE, related_name="brackets")
    lower_limit = models.DecimalField(**MONEY)
    base_tax = models.DecimalField(**MONEY)
    rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percent.")

    class Meta:
        ordering = ["table_id", "lower_limit"]
        constraints = [
            models.UniqueConstraint(fields=["table", "lower_limit"], name="unique_bracket_per_table"),
        ]

    def __str__(self):
        return f"{self.table.tax_year}: above {self.lower_limit} -> {self.base_tax} + {self.rate}%"


class PayrollSetting(AuditedModel):
    """Per company code: the UIF and SDL rates and ceiling, and the G/L accounts payroll posts to."""

    company_code = models.OneToOneField("organization.CompanyCode", on_delete=models.PROTECT,
                                        related_name="payroll_setting")
    uif_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1.00"),
                                   help_text="Percent, paid by the employee and again by the employer.")
    uif_monthly_ceiling = models.DecimalField(**MONEY, default=Decimal("17712.00"),
                                              help_text="UIF is calculated on remuneration up to this amount.")
    sdl_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1.00"),
                                   help_text="Skills development levy, percent, employer only. 0 if exempt.")
    salary_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    uif_expense_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    sdl_expense_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    paye_payable_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    uif_payable_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    sdl_payable_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    net_pay_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+",
                                        help_text="Salaries payable to employees until they are paid.")

    def __str__(self):
        return f"Payroll settings {self.company_code.code}"


class PayrollRun(AuditedModel):
    """One posted month of payroll with its totals and journal entry. Reversed, not edited."""

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    run_number = models.CharField(max_length=12)
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    period_start = models.DateField()
    period_end = models.DateField()
    posting_date = models.DateField()
    tax_year = models.PositiveSmallIntegerField()
    gross = models.DecimalField(**MONEY)
    paye = models.DecimalField(**MONEY)
    uif_employee = models.DecimalField(**MONEY)
    uif_employer = models.DecimalField(**MONEY)
    sdl = models.DecimalField(**MONEY)
    net = models.DecimalField(**MONEY)
    journal_entry = models.OneToOneField("finance.JournalEntry", on_delete=models.PROTECT, related_name="payroll_run")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.POSTED)

    class Meta:
        ordering = ["-year", "-month", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "run_number"], name="unique_payroll_run_number"),
            models.UniqueConstraint(fields=["company_code", "year", "month"], condition=Q(status="POSTED"),
                                    name="one_posted_payroll_per_month"),
        ]

    def __str__(self):
        return f"{self.run_number} {self.year}-{self.month:02d} ({self.status})"


class PayrollResult(models.Model):
    """One employee's calculated amounts in a payroll run."""

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="results")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="payroll_results")
    cost_center = models.ForeignKey("controlling.CostCenter", on_delete=models.PROTECT, related_name="+")
    gross = models.DecimalField(**MONEY)
    paye = models.DecimalField(**MONEY)
    uif_employee = models.DecimalField(**MONEY)
    uif_employer = models.DecimalField(**MONEY)
    sdl = models.DecimalField(**MONEY)
    net = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["run_id", "employee__personnel_number"]

    def __str__(self):
        return f"{self.run.run_number}: {self.employee.personnel_number} net {self.net}"
