"""Payroll rules (South Africa) and its posting to the Universal Journal.

Per employee and month:
    PAYE  = (tax on gross x 12 under the year's SARS table - primary rebate) / 12, never below zero
    UIF   = uif_rate% of gross up to the monthly ceiling, once for the employee and once for the employer
    SDL   = sdl_rate% of gross, employer only
    net   = gross - PAYE - employee UIF

One payroll run per company code and month posts one journal entry (document type PY):
    Dr salaries, employer UIF and SDL           per cost centre (and so per profit centre)
    Cr PAYE, UIF (employee + employer) and SDL  owed to SARS
    Cr salaries payable (net)                   owed to the employees until the salary payment

Simplifications for now: monthly salary only (no allowances, overtime, medical aid or retirement
funding), primary rebate only (no age rebates), no proration for part months, SDL applied whenever
its rate is above zero.
"""
import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
import logging

from django.db import transaction

from apps.finance import services as finance
from apps.finance.models import DocumentType, JournalLine

from .models import Employee, PayeTable, PayrollResult, PayrollRun, PayrollSetting

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
MONTHS = Decimal("12")
CONTINUOUS = 0


class PayrollError(finance.PostingError):
    """The request breaks a payroll rule and nothing was saved."""


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def tax_year_for(day: date) -> int:
    """SARS year of assessment: 1 March to the end of February, named after the year it ends in."""
    return day.year + 1 if day.month >= 3 else day.year


# --- Employees ---------------------------------------------------------------------------

@transaction.atomic
def create_employee(*, company_code, position, first_name: str, last_name: str, hire_date: date,
                    monthly_salary: Decimal, user=None, **extra) -> Employee:
    """Hire an employee into a position; the personnel number comes from the gap-free "PN" range."""
    if position.org_unit.company_code_id != company_code.pk:
        raise PayrollError(f"Position {position.code} is not in company code {company_code.code}.")
    if monthly_salary is None or monthly_salary < 0:
        raise PayrollError("The monthly salary cannot be negative.")
    employee = Employee.objects.create(
        company_code=company_code, personnel_number=finance.next_document_number(company_code, CONTINUOUS, "PN"),
        position=position, first_name=first_name, last_name=last_name, hire_date=hire_date,
        monthly_salary=monthly_salary, created_by=user, updated_by=user, **extra,
    )
    # Personnel number and position only: names and salary are personal data and stay out of logs.
    logger.info("Hired employee %s into position %s", employee.personnel_number, position.code)
    return employee


# --- Calculation ---------------------------------------------------------------------------

def annual_tax(table: PayeTable, income: Decimal) -> Decimal:
    """Tax on annual taxable income before rebates."""
    bracket = table.brackets.filter(lower_limit__lt=income).order_by("-lower_limit").first()
    if bracket is None:
        return ZERO
    return bracket.base_tax + (income - bracket.lower_limit) * bracket.rate / HUNDRED


def monthly_paye(table: PayeTable, monthly_gross: Decimal) -> Decimal:
    """PAYE for one month: tax on the annualised salary, less the primary rebate, divided by 12."""
    tax = annual_tax(table, monthly_gross * MONTHS) - table.primary_rebate
    return _round(max(tax, ZERO) / MONTHS)


def calculate(employee: Employee, setting: PayrollSetting, table: PayeTable) -> dict:
    """Gross, PAYE, UIF (employee and employer), SDL and net pay for one employee for one month."""
    gross = _round(employee.monthly_salary)
    uif = _round(min(gross, setting.uif_monthly_ceiling) * setting.uif_rate / HUNDRED)
    paye = monthly_paye(table, gross)
    return {
        "gross": gross, "paye": paye, "uif_employee": uif, "uif_employer": uif,
        "sdl": _round(gross * setting.sdl_rate / HUNDRED), "net": gross - paye - uif,
    }


def _setting(company_code) -> PayrollSetting:
    setting = PayrollSetting.objects.select_related(
        "salary_account", "uif_expense_account", "sdl_expense_account", "paye_payable_account",
        "uif_payable_account", "sdl_payable_account", "net_pay_account",
    ).filter(company_code=company_code).first()
    if setting is None:
        raise PayrollError(f"Payroll is not set up for company code {company_code.code}.")
    return setting


def _table(tax_year: int) -> PayeTable:
    table = PayeTable.objects.filter(tax_year=tax_year).first()
    if table is None:
        raise PayrollError(f"There is no PAYE table for tax year {tax_year}; load the SARS tables first.")
    return table


def employees_in_period(company_code, start: date, end: date):
    """Everyone employed at any time between ``start`` and ``end`` (hired by the end,
    not terminated before the start), with the data needed for cost centres.
    """
    return (
        Employee.objects.select_related("position__cost_center__profit_center",
                                        "position__org_unit__cost_center__profit_center")
        .filter(company_code=company_code, hire_date__lte=end)
        .exclude(termination_date__lt=start)
        .order_by("personnel_number")
    )


# --- Posting -------------------------------------------------------------------------------

def _journal_lines(setting, results) -> list[finance.LineInput]:
    debit, credit = JournalLine.Side.DEBIT, JournalLine.Side.CREDIT
    costs = {}  # cost centre -> [gross, employer UIF, SDL]
    totals = dict.fromkeys(("paye", "uif_employee", "uif_employer", "sdl", "net"), ZERO)
    for cost_center, amounts in results:
        row = costs.setdefault(cost_center, [ZERO, ZERO, ZERO])
        row[0] += amounts["gross"]
        row[1] += amounts["uif_employer"]
        row[2] += amounts["sdl"]
        for key in totals:
            totals[key] += amounts[key]

    lines = []
    for cost_center, (gross, uif, sdl) in sorted(costs.items(), key=lambda item: item[0].code):
        for account, amount, text in ((setting.salary_account, gross, "Salaries"),
                                      (setting.uif_expense_account, uif, "UIF employer contribution"),
                                      (setting.sdl_expense_account, sdl, "Skills development levy")):
            if amount:
                lines.append(finance.LineInput(account, debit, amount, cost_center=cost_center,
                                               text=f"{text} {cost_center.code}"))
    uif_total = totals["uif_employee"] + totals["uif_employer"]
    for account, amount, text in ((setting.paye_payable_account, totals["paye"], "PAYE payable"),
                                  (setting.uif_payable_account, uif_total, "UIF payable"),
                                  (setting.sdl_payable_account, totals["sdl"], "SDL payable"),
                                  (setting.net_pay_account, totals["net"], "Net salaries payable")):
        if amount:
            lines.append(finance.LineInput(account, credit, amount, text=text))
    return lines


@transaction.atomic
def post_payroll_run(company_code, *, year: int, month: int, posting_date: date | None = None,
                     user=None) -> PayrollRun:
    """Calculate payroll for everyone employed during the month and post it. All or nothing."""
    if not 1 <= month <= 12:
        raise PayrollError("Month must be between 1 and 12.")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    posting_date = posting_date or end
    # Lock the company's payroll setting so two runs for the same month cannot post at once.
    setting = _setting(company_code)
    PayrollSetting.objects.select_for_update().filter(pk=setting.pk).first()
    if PayrollRun.objects.filter(company_code=company_code, year=year, month=month,
                                 status=PayrollRun.Status.POSTED).exists():
        raise PayrollError(f"Payroll for {year}-{month:02d} is already posted; reverse it to run it again.")
    tax_year = tax_year_for(start)
    table = _table(tax_year)

    employees = list(employees_in_period(company_code, start, end))
    if not employees:
        raise PayrollError(f"Nobody in company code {company_code.code} is employed in {year}-{month:02d}.")
    results = []
    for employee in employees:
        cost_center = employee.position.effective_cost_center
        if cost_center is None:
            raise PayrollError(
                f"Employee {employee.personnel_number} has no cost centre: set one on position "
                f"{employee.position.code} or org unit {employee.position.org_unit.code}."
            )
        results.append((employee, cost_center, calculate(employee, setting, table)))

    run_number = finance.next_document_number(company_code, CONTINUOUS, "PR")
    lines = _journal_lines(setting, [(cost_center, amounts) for _, cost_center, amounts in results])
    entry = finance.post_journal_entry(
        company_code=company_code, document_type=DocumentType.PAYROLL, posting_date=posting_date,
        reference=run_number, header_text=f"Payroll {year}-{month:02d}", lines=lines, user=user,
    )
    totals = {key: sum((amounts[key] for _, _, amounts in results), ZERO)
              for key in ("gross", "paye", "uif_employee", "uif_employer", "sdl", "net")}
    run = PayrollRun.objects.create(
        company_code=company_code, run_number=run_number, year=year, month=month, period_start=start,
        period_end=end, posting_date=posting_date, tax_year=tax_year, journal_entry=entry,
        created_by=user, updated_by=user, **totals,
    )
    PayrollResult.objects.bulk_create([
        PayrollResult(run=run, employee=employee, cost_center=cost_center, **amounts)
        for employee, cost_center, amounts in results
    ])
    logger.info("Posted payroll run %s for %s-%02d: %d employee(s), journal entry %s",
                run.run_number, year, month, len(results), entry.document_number)
    return run


@transaction.atomic
def reverse_payroll_run(run: PayrollRun, *, posting_date: date | None = None, user=None) -> PayrollRun:
    """Reverse the payroll journal entry so the month can be run again (e.g. after a salary correction)."""
    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    if run.status != PayrollRun.Status.POSTED:
        raise PayrollError(f"Payroll run {run.run_number} is already reversed.")
    finance.reverse_journal_entry(run.journal_entry, posting_date=posting_date, user=user)
    PayrollRun.objects.filter(pk=run.pk).update(status=PayrollRun.Status.REVERSED, updated_by=user)
    run.refresh_from_db()
    logger.info("Reversed payroll run %s", run.run_number)
    return run
