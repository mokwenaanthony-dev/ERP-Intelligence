"""Controlling reports. Like every other report, they are sums over Universal Journal lines."""
from decimal import Decimal

from django.db.models import Sum

from apps.finance.models import GLAccount, JournalLine

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
REVENUE, EXPENSE = GLAccount.AccountType.REVENUE, GLAccount.AccountType.EXPENSE


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _lines(company_code, fiscal_year, period_from, period_to):
    return JournalLine.objects.filter(
        company_code=company_code, fiscal_year=fiscal_year, period__gte=period_from, period__lte=period_to,
    )


def cost_center_report(company_code, fiscal_year: int, period_from: int = 1, period_to: int = 12,
                       cost_center=None) -> dict:
    """Actual costs per cost centre and cost element (expense account), debits positive."""
    lines = _lines(company_code, fiscal_year, period_from, period_to).filter(cost_center__isnull=False)
    if cost_center is not None:
        lines = lines.filter(cost_center=cost_center)
    rows = (
        lines.values("cost_center__code", "cost_center__name", "gl_account__number", "gl_account__name")
        .annotate(total=Sum("amount_local")).order_by("cost_center__code", "gl_account__number")
    )
    centers = {}
    for row in rows:
        center = centers.setdefault(row["cost_center__code"], {
            "cost_center": row["cost_center__code"], "name": row["cost_center__name"], "accounts": [], "total": ZERO,
        })
        amount = _money(row["total"])
        center["accounts"].append({"gl_account": row["gl_account__number"], "name": row["gl_account__name"],
                                   "amount": amount})
        center["total"] += amount
    result = list(centers.values())
    return {
        "company_code": company_code.code, "currency": company_code.currency.code, "fiscal_year": fiscal_year,
        "period_from": period_from, "period_to": period_to, "cost_centers": result,
        "total": sum((c["total"] for c in result), ZERO),
    }


def profit_center_report(company_code, fiscal_year: int, period_from: int = 1, period_to: int = 12) -> dict:
    """Revenue, expenses and result per profit centre. Lines without one show as unassigned."""
    rows = (
        _lines(company_code, fiscal_year, period_from, period_to)
        .filter(gl_account__account_type__in=[REVENUE, EXPENSE])
        .values("profit_center__code", "profit_center__name", "gl_account__account_type")
        .annotate(total=Sum("amount_local"))
    )
    centers = {}
    for row in rows:
        code = row["profit_center__code"]
        center = centers.setdefault(code, {
            "profit_center": code, "name": row["profit_center__name"] or "Unassigned",
            "revenue": ZERO, "expenses": ZERO, "result": ZERO,
        })
        amount = _money(row["total"])
        if row["gl_account__account_type"] == REVENUE:
            center["revenue"] += -amount  # revenue is a credit
        else:
            center["expenses"] += amount
    for center in centers.values():
        center["result"] = center["revenue"] - center["expenses"]
    result = sorted(centers.values(), key=lambda c: (c["profit_center"] is None, c["profit_center"] or ""))
    totals = {key: sum((c[key] for c in result), ZERO) for key in ("revenue", "expenses", "result")}
    return {
        "company_code": company_code.code, "currency": company_code.currency.code, "fiscal_year": fiscal_year,
        "period_from": period_from, "period_to": period_to, "profit_centers": result, "totals": totals,
    }
