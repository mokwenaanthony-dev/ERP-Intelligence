"""Management reports on the reporting views.

Every report says how current its numbers are: ``materialized`` is True on PostgreSQL, where the
views are refreshed by ``refresh_views`` (Celery, every 15 minutes, or on demand), and
``data_as_of`` is the time of the last refresh. On SQLite the views are live.
"""
import time
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import connection
from django.db.models import Q, Sum
from django.utils import timezone

from apps.finance.models import GLAccount
from apps.inventory.models import AccountKey, InventoryAccountSetting

from .models import GLPeriodBalance, PayrollCost, RefreshLog, SalesMargin, StockPosition
from .sql import VIEWS, is_postgres

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
QTY = Decimal("0.001")
REVENUE, EXPENSE = GLAccount.AccountType.REVENUE, GLAccount.AccountType.EXPENSE
ASSET, LIABILITY, EQUITY = GLAccount.AccountType.ASSET, GLAccount.AccountType.LIABILITY, GLAccount.AccountType.EQUITY


def _money(value) -> Decimal:
    # SQLite sums decimals as floats, so every total is rounded back to cents.
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(part: Decimal, whole: Decimal) -> Decimal | None:
    return (part * 100 / whole).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if whole else None


# --- Refresh and freshness ----------------------------------------------------------------

def refresh_views() -> list[dict]:
    """Refresh every materialized view without blocking readers. Nothing to do on SQLite."""
    if not is_postgres(connection):
        return []
    results = []
    with connection.cursor() as cursor:
        for name in VIEWS:
            started = time.monotonic()
            cursor.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {name}")
            duration = int((time.monotonic() - started) * 1000)
            log, _ = RefreshLog.objects.update_or_create(
                view_name=name, defaults={"refreshed_at": timezone.now(), "duration_ms": duration}
            )
            results.append({"view": name, "refreshed_at": log.refreshed_at, "duration_ms": duration})
    return results


def _freshness(*views: str) -> dict:
    materialized = is_postgres(connection)
    as_of = None
    if materialized:
        times = list(RefreshLog.objects.filter(view_name__in=views).values_list("refreshed_at", flat=True))
        as_of = min(times) if len(times) == len(views) else None
    return {"materialized": materialized, "data_as_of": as_of}


def freshness() -> dict:
    return {
        "materialized": is_postgres(connection),
        "views": [{"view": name, "refreshed_at": log.refreshed_at if log else None,
                   "duration_ms": log.duration_ms if log else None}
                  for name in VIEWS for log in [RefreshLog.objects.filter(view_name=name).first()]],
    }


def _header(company_code, *views: str, **extra) -> dict:
    return {"company_code": company_code.code, "currency": company_code.currency.code, **extra,
            **_freshness(*views)}


# --- Income statement and balance sheet ----------------------------------------------------

def cost_of_sales_accounts(company_code) -> set[int]:
    return set(InventoryAccountSetting.objects.filter(
        chart_of_accounts=company_code.chart_of_accounts, account_key=AccountKey.COST_OF_SALES
    ).values_list("gl_account_id", flat=True))


def income_statement(company_code, fiscal_year: int, period_from: int = 1, period_to: int = 12,
                     profit_center=None) -> dict:
    """Revenue, cost of sales, gross margin, operating expenses and result, with a per-period trend."""
    rows = GLPeriodBalance.objects.filter(
        company_code=company_code, fiscal_year=fiscal_year, period__gte=period_from, period__lte=period_to,
        account_type__in=[REVENUE, EXPENSE],
    )
    if profit_center is not None:
        rows = rows.filter(profit_center=profit_center)
    rows = rows.values("period", "gl_account_id", "gl_account__number", "gl_account__name", "account_type") \
        .annotate(total=Sum("balance"))
    cogs = cost_of_sales_accounts(company_code)

    sections = {"revenue": {}, "cost_of_sales": {}, "operating_expenses": {}}
    periods = {p: dict.fromkeys(("revenue", "cost_of_sales", "operating_expenses"), ZERO)
               for p in range(period_from, period_to + 1)}
    for row in rows:
        amount = _money(row["total"])
        if row["account_type"] == REVENUE:
            section, amount = "revenue", -amount  # revenue is a credit
        elif row["gl_account_id"] in cogs:
            section = "cost_of_sales"
        else:
            section = "operating_expenses"
        line = sections[section].setdefault(row["gl_account__number"], {
            "gl_account": row["gl_account__number"], "name": row["gl_account__name"], "amount": ZERO,
        })
        line["amount"] += amount
        periods[row["period"]][section] += amount

    totals = {name: sum((line["amount"] for line in lines.values()), ZERO) for name, lines in sections.items()}
    gross_margin = totals["revenue"] - totals["cost_of_sales"]
    trend = []
    for period, values in periods.items():
        margin = values["revenue"] - values["cost_of_sales"]
        trend.append({"period": period, **values, "gross_margin": margin,
                      "result": margin - values["operating_expenses"]})
    return {
        **_header(company_code, "analytics_gl_period_balance", fiscal_year=fiscal_year, period_from=period_from,
                  period_to=period_to, profit_center=profit_center.code if profit_center else None),
        **{name: sorted(lines.values(), key=lambda line: line["gl_account"]) for name, lines in sections.items()},
        "totals": {**totals, "gross_margin": gross_margin, "gross_margin_percent": _percent(gross_margin,
                                                                                            totals["revenue"]),
                   "result": gross_margin - totals["operating_expenses"]},
        "by_period": trend,
    }


def balance_sheet(company_code, fiscal_year: int, period: int) -> dict:
    """Balances at the end of a period: everything posted up to then, in any earlier year too.

    Until year-end closing exists, the result of all periods so far is shown inside equity as
    "Retained earnings and result to date", so assets always equal liabilities plus equity.
    """
    rows = GLPeriodBalance.objects.filter(company_code=company_code).filter(
        Q(fiscal_year__lt=fiscal_year) | Q(fiscal_year=fiscal_year, period__lte=period)
    ).values("gl_account__number", "gl_account__name", "account_type").annotate(total=Sum("balance"))
    sections = {ASSET: [], LIABILITY: [], EQUITY: []}
    result = ZERO
    for row in rows:
        amount = _money(row["total"])
        if row["account_type"] in (REVENUE, EXPENSE):
            result -= amount
            continue
        if amount == 0:
            continue
        sign = 1 if row["account_type"] == ASSET else -1  # liabilities and equity are credits
        sections[row["account_type"]].append({"gl_account": row["gl_account__number"],
                                              "name": row["gl_account__name"], "amount": amount * sign})
    for lines in sections.values():
        lines.sort(key=lambda line: line["gl_account"])
    sections[EQUITY].append({"gl_account": None, "name": "Retained earnings and result to date", "amount": result})
    totals = {key.lower(): sum((line["amount"] for line in lines), ZERO) for key, lines in sections.items()}
    return {
        **_header(company_code, "analytics_gl_period_balance", fiscal_year=fiscal_year, period=period),
        "assets": sections[ASSET], "liabilities": sections[LIABILITY], "equity": sections[EQUITY],
        "totals": {"assets": totals["asset"], "liabilities": totals["liability"], "equity": totals["equity"],
                   "liabilities_and_equity": totals["liability"] + totals["equity"]},
        "balanced": totals["asset"] == totals["liability"] + totals["equity"],
    }


# --- Sales margin --------------------------------------------------------------------------

MARGIN_GROUPS = {
    "customer": ("business_partner__number", "business_partner__name"),
    "material": ("material__number", "material__description"),
    "profit_center": ("profit_center__code", "profit_center__name"),
}


def sales_margin(company_code, fiscal_year: int, period_from: int = 1, period_to: int = 12,
                 group_by: str = "customer") -> dict:
    """Revenue, cost of sales and gross margin by customer, material or profit centre."""
    if group_by not in MARGIN_GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(MARGIN_GROUPS)}.")
    key_field, name_field = MARGIN_GROUPS[group_by]
    rows = SalesMargin.objects.filter(
        company_code=company_code, fiscal_year=fiscal_year, period__gte=period_from, period__lte=period_to,
    ).values(key_field, name_field).annotate(revenue=Sum("revenue"), cost_of_sales=Sum("cost_of_sales"))
    result = []
    for row in rows:
        revenue, cost = _money(row["revenue"]), _money(row["cost_of_sales"])
        margin = revenue - cost
        result.append({"key": row[key_field], "name": row[name_field] or "Not assigned", "revenue": revenue,
                       "cost_of_sales": cost, "gross_margin": margin, "margin_percent": _percent(margin, revenue)})
    result.sort(key=lambda r: (-r["revenue"], r["key"] or ""))
    revenue = sum((r["revenue"] for r in result), ZERO)
    cost = sum((r["cost_of_sales"] for r in result), ZERO)
    return {
        **_header(company_code, "analytics_sales_margin", fiscal_year=fiscal_year, period_from=period_from,
                  period_to=period_to, group_by=group_by),
        "rows": result,
        "totals": {"revenue": revenue, "cost_of_sales": cost, "gross_margin": revenue - cost,
                   "margin_percent": _percent(revenue - cost, revenue)},
    }


# --- Stock ageing --------------------------------------------------------------------------

AGE_BUCKETS = (("0-30", 30), ("31-90", 90), ("91-180", 180), ("over_180", None))


def _age_bucket(days: int | None) -> str:
    if days is None:
        return "no_receipt"
    for name, limit in AGE_BUCKETS:
        if limit is None or days <= limit:
            return name
    return "over_180"  # pragma: no cover - the last bucket has no limit


def stock_ageing(company_code, as_of: date | None = None, slow_after_days: int = 90) -> dict:
    """Stock on hand by material and plant, aged by days since the last receipt.

    A line is *slow moving* when nothing has been issued for more than ``slow_after_days``.
    Quantities are current; ``as_of`` only sets the date ages are counted from.
    """
    as_of = as_of or date.today()
    rows = StockPosition.objects.filter(company_code=company_code).select_related("material", "plant") \
        .order_by("material__number", "plant__code")
    result = []
    buckets = dict.fromkeys([name for name, _ in AGE_BUCKETS] + ["no_receipt"], ZERO)
    for row in rows:
        quantity = Decimal(str(row.quantity or 0)).quantize(QTY)
        if quantity == 0:
            continue
        value = _money(row.value)
        age = (as_of - row.last_receipt_date).days if row.last_receipt_date else None
        idle = (as_of - row.last_issue_date).days if row.last_issue_date else None
        bucket = _age_bucket(age)
        buckets[bucket] += value
        result.append({
            "material": row.material.number, "description": row.material.description, "plant": row.plant.code,
            "quantity": quantity, "value": value, "last_receipt_date": row.last_receipt_date, "age_days": age,
            "age_bucket": bucket, "last_issue_date": row.last_issue_date,
            "slow_moving": idle is None or idle > slow_after_days,
        })
    return {
        **_header(company_code, "analytics_stock_position", as_of=as_of, slow_after_days=slow_after_days),
        "rows": result, "value_by_age": buckets, "total_value": sum(buckets.values(), ZERO),
        "slow_moving_value": sum((r["value"] for r in result if r["slow_moving"]), ZERO),
    }


# --- Payroll cost --------------------------------------------------------------------------

def payroll_cost_trend(company_code, year: int | None = None, by_cost_center: bool = False) -> dict:
    """Posted payroll cost per month (optionally per cost centre): headcount, gross, employer costs."""
    rows = PayrollCost.objects.filter(company_code=company_code)
    if year is not None:
        rows = rows.filter(year=year)
    fields = ["year", "month"] + (["cost_center__code"] if by_cost_center else [])
    rows = rows.values(*fields).annotate(
        headcount=Sum("headcount"), gross=Sum("gross"), employer_contributions=Sum("employer_contributions"),
        total_cost=Sum("total_cost"),
    ).order_by(*fields)
    result = []
    for row in rows:
        line = {"year": row["year"], "month": row["month"], "headcount": row["headcount"],
                "gross": _money(row["gross"]), "employer_contributions": _money(row["employer_contributions"]),
                "total_cost": _money(row["total_cost"])}
        if by_cost_center:
            line["cost_center"] = row["cost_center__code"]
        result.append(line)
    return {
        **_header(company_code, "analytics_payroll_cost", year=year, by_cost_center=by_cost_center),
        "rows": result, "total_cost": sum((r["total_cost"] for r in result), ZERO),
    }
