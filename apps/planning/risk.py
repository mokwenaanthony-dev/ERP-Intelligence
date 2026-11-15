"""Supply-chain risk and forecast accuracy (Wave 8c): the "future state" lines of the north star.

**Revenue at risk per supplier** answers: *if this supplier failed, which open customer orders could
we not deliver, and how much are they worth?*

1. What each supplier supplies to the company: materials on its non-cancelled purchase orders, on its
   active contracts, or where it is the preferred supplier in the planning parameters.
2. A material is **single source** when only one supplier supplies it.
3. What depends on each material: the material itself and every product that uses it through the
   active BOMs of any plant, at any depth (where-used).
4. The open sales order lines (open quantity x net price) for those products, wanted within the
   horizon, are the supplier's **exposed revenue**; those that depend on a material only this
   supplier supplies are its **revenue at risk**.
5. A risk level and a recommendation, from rules a person can read: HIGH when revenue is at risk and
   the supplier is graded C or D (or not yet evaluated) or the material has no active contract;
   MEDIUM when revenue is at risk otherwise; LOW when every material has a second source. The
   recommendation is a proposal (qualify a second source through an RFQ, sign a contract, evaluate
   the supplier); people decide (north star safeguard).

**Forecast accuracy** answers: *how good were the forecasts we planned with?* For each past month, the
forecast that was available at the start of that month (the latest run whose first month is not
after it) is compared with what was actually delivered (``services.demand_history``): mean absolute
error (MAE), bias (positive = forecast too high) and accuracy = 1 - total absolute error / total
actual, never below zero. Predictions show their track record (north star safeguard).

Both are read-only calculations over the ERP's tables, so they are always current and need no
storage. Computed in the service layer rather than in Cypher so that they are tested end to end
(ADR 0016).
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from apps.masterdata.models import BusinessPartner, Material
from apps.production import services as production
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem
from apps.sales.models import SalesOrder, SalesOrderItem
from apps.srm.models import Contract, ContractItem, SupplierEvaluation

from .models import ForecastPeriod, MaterialPlanning
from .services import add_months, demand_history, month_start

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
WEAK_GRADES = {"C", "D", None}


# --- revenue at risk ---------------------------------------------------------------------------------

def _sources(company_code, on: date) -> dict[int, set[int]]:
    """Material id -> the ids of the suppliers that supply it to the company code."""
    sources: dict[int, set[int]] = {}
    for material_id, supplier_id in (PurchaseOrderItem.objects
                                     .filter(order__company_code=company_code)
                                     .exclude(order__status=PurchaseOrder.Status.CANCELLED)
                                     .values_list("material_id", "order__supplier_id").distinct()):
        sources.setdefault(material_id, set()).add(supplier_id)
    for material_id, supplier_id in (ContractItem.objects
                                     .filter(contract__company_code=company_code, contract__status="ACTIVE",
                                             contract__valid_to__gte=on)
                                     .values_list("material_id", "contract__supplier_id")):
        sources.setdefault(material_id, set()).add(supplier_id)
    for material_id, supplier_id in (MaterialPlanning.objects
                                     .filter(plant__company_code=company_code, preferred_supplier__isnull=False)
                                     .values_list("material_id", "preferred_supplier_id")):
        sources.setdefault(material_id, set()).add(supplier_id)
    return sources


def _used_in(company_code) -> dict[int, set[int]]:
    """Material id -> every material that uses it through active BOMs (any plant, any depth), itself included."""
    parents: dict[int, set[int]] = {}
    from apps.organization.models import Plant

    for plant in Plant.objects.filter(company_code=company_code):
        for parent, children in production._bom_children(plant).items():
            for child in children:
                parents.setdefault(child, set()).add(parent)
    closure: dict[int, set[int]] = {}

    def up(material_id: int) -> set[int]:
        """The material and everything above it in the BOMs (memoised)."""
        if material_id not in closure:
            closure[material_id] = {material_id}  # set first: a cycle (refused by the BOM rules) cannot loop
            for parent in parents.get(material_id, ()):
                closure[material_id] |= up(parent)
        return closure[material_id]

    for material_id in list(parents):
        up(material_id)
    return closure


def supplier_risk(company_code, *, on: date, months: int = 3) -> list[dict]:
    """Per supplier: what it supplies, what depends on it, the revenue at risk, a level and a recommendation.

    Ordered by risk (HIGH first) and revenue at risk. ``months`` limits the open sales orders counted
    to those wanted before the end of that many months from ``on``'s month.
    """
    horizon_end = add_months(month_start(on), months)
    sources = _sources(company_code, on)
    used_in = _used_in(company_code)
    grades = {}
    for evaluation in SupplierEvaluation.objects.filter(company_code=company_code).order_by("period_end", "id"):
        grades[evaluation.supplier_id] = evaluation.grade  # the latest evaluation wins
    contracted = set(Contract.objects.filter(company_code=company_code, status="ACTIVE", valid_from__lte=on,
                                             valid_to__gte=on).values_list("items__material_id", "supplier_id"))

    # Open sales value per material (open quantity x net price), wanted within the horizon.
    open_value: dict[int, Decimal] = {}
    open_orders: dict[int, set[str]] = {}
    for item in (SalesOrderItem.objects.filter(order__company_code=company_code,
                                               order__status__in=[SalesOrder.Status.OPEN, SalesOrder.Status.CONFIRMED])
                 .select_related("order")):
        wanted = item.order.requested_delivery_date or item.order.order_date
        open_quantity = item.open_quantity
        if open_quantity > 0 and wanted < horizon_end:
            value = open_quantity * item.net_price
            open_value[item.material_id] = open_value.get(item.material_id, Decimal("0")) + value
            open_orders.setdefault(item.material_id, set()).add(item.order.order_number)

    suppliers = {s.pk: s for s in BusinessPartner.objects.filter(pk__in={s for ids in sources.values() for s in ids})}
    materials = {m.pk: m for m in Material.objects.filter(pk__in=sources)}
    rows = []
    for supplier_id, supplier in suppliers.items():
        supplied = sorted((m for m, ids in sources.items() if supplier_id in ids), key=lambda m: materials[m].number)
        exposed, at_risk = set(), set()  # product ids whose open orders depend on this supplier
        lines = []
        for material_id in supplied:
            single = len(sources[material_id]) == 1
            dependants = used_in.get(material_id, {material_id})
            exposed |= dependants
            if single:
                at_risk |= dependants
            lines.append({"material": materials[material_id].number, "single_source": single,
                          "on_contract": (material_id, supplier_id) in contracted,
                          "used_in": sorted(Material.objects.filter(pk__in=dependants - {material_id})
                                            .values_list("number", flat=True))})
        exposed_value = sum((open_value.get(p, Decimal("0")) for p in exposed), Decimal("0")).quantize(CENT)
        risk_value = sum((open_value.get(p, Decimal("0")) for p in at_risk), Decimal("0")).quantize(CENT)
        orders = sorted({o for p in at_risk for o in open_orders.get(p, ())})
        grade = grades.get(supplier_id)
        uncovered = [line["material"] for line in lines if line["single_source"] and not line["on_contract"]]
        level, advice = _assess(risk_value, grade, uncovered, lines)
        rows.append({"supplier": supplier.number, "name": supplier.name, "grade": grade, "materials": lines,
                     "exposed_revenue": exposed_value, "revenue_at_risk": risk_value, "orders_at_risk": orders,
                     "risk": level, "recommendation": advice})
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    rows.sort(key=lambda r: (order[r["risk"]], -r["revenue_at_risk"], r["supplier"]))
    logger.info("Supplier risk for %s from %s, %d month(s): %d supplier(s), %d high, revenue at risk %s",
                company_code.code, on, months, len(rows), sum(r["risk"] == "HIGH" for r in rows),
                sum((r["revenue_at_risk"] for r in rows), Decimal("0")))
    return rows


def _assess(risk_value: Decimal, grade, uncovered: list[str], lines: list[dict]) -> tuple[str, str]:
    """Risk level and a readable recommendation from the rules in the module description."""
    single = [line["material"] for line in lines if line["single_source"]]
    if risk_value <= 0:
        if single:
            return "LOW", f"Sole source of {', '.join(single)}, but no open orders depend on it in the horizon."
        return "LOW", "Every material it supplies has another source."
    advice = [f"Qualify a second source for {', '.join(single)} (run an RFQ)."]
    if uncovered:
        advice.append(f"Agree a contract for {', '.join(uncovered)}.")
    if grade is None:
        advice.append("Evaluate the supplier.")
    elif grade in WEAK_GRADES:
        advice.append(f"Grade {grade}: review its performance with the supplier.")
    level = "HIGH" if grade in WEAK_GRADES or uncovered else "MEDIUM"
    return level, " ".join(advice)


# --- forecast accuracy -----------------------------------------------------------------------------------

def forecast_accuracy(plant, *, on: date, months: int = 6) -> list[dict]:
    """Per material: each past month's forecast against actual deliveries, and MAE, bias and accuracy.

    Looks at the ``months`` complete months before ``on``'s month. A month counts only if a forecast
    for it existed at its start; materials with no such month are left out.
    """
    current = month_start(on)
    first = add_months(current, -months)
    periods = (ForecastPeriod.objects.filter(item__run__plant=plant, period__gte=first, period__lt=current)
               .select_related("item__material", "item__run")
               .order_by("item__material__number", "period", "-item__run__first_period", "-item__run__id"))
    chosen: dict[tuple[int, date], ForecastPeriod] = {}
    for period in periods:
        if period.item.run.first_period > period.period:
            continue  # made after the month started: not what was planned with
        chosen.setdefault((period.item.material_id, period.period), period)
    if not chosen:
        return []
    materials = sorted({p.item.material for p in chosen.values()}, key=lambda m: m.number)
    actual = {h.material.pk: dict(zip([add_months(first, i) for i in range(months)], h.quantities))
              for h in demand_history(plant, current, months, materials)}
    rows = []
    for material in materials:
        points = []
        for month in (add_months(first, i) for i in range(months)):
            period = chosen.get((material.pk, month))
            if period is None:
                continue
            real = actual[material.pk][month]
            points.append({"period": f"{month:%Y-%m}", "forecast": period.quantity, "actual": real,
                           "error": (period.quantity - real).quantize(Decimal("0.001")),
                           "run": period.item.run.run_number})
        total_actual = sum((p["actual"] for p in points), Decimal("0"))
        total_error = sum((abs(p["error"]) for p in points), Decimal("0"))
        mae = (total_error / len(points)).quantize(Decimal("0.001"))
        bias = (sum((p["error"] for p in points), Decimal("0")) / len(points)).quantize(Decimal("0.001"))
        accuracy = (max(Decimal("0"), 1 - total_error / total_actual) * 100).quantize(Decimal("0.1")) \
            if total_actual > 0 else None
        rows.append({"material": material.number, "description": material.description, "months": len(points),
                     "mae": mae, "bias": bias, "accuracy_percent": accuracy, "points": points})
    return rows
