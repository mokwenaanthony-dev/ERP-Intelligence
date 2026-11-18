"""Control-tower calculations (Wave 9a, ADR 0017): KPIs now and ahead, and the problems inbox.

**KPIs** (the business results of the north star, ``docs/VISION.md``), each with its value, target,
a status (good / warning / critical) and how it was measured:

=========================  ==================================================================================
On time in full, customers Sales order lines due in the last ``days`` days that were completely delivered
                           (posted deliveries) on or before their requested date, as a share of all such lines.
On time in full, suppliers Purchase order lines due in the last ``days`` days that were completely received
                           (posted goods receipts) on or before their delivery date.
Forecast accuracy          1 - total absolute error / total actual demand over the last six complete months,
                           all plants, each month judged with the forecast available at its start (8c).
Inventory days             Stock value today / (cost of sales of the last ``days`` days / ``days``): how many
                           days the stock would last at the current rate of sales.
Revenue at risk            Open sales value in the next ``months`` months that depends on a single-source
                           supplier (8c).
=========================  ==================================================================================

The *future state* beside them: the open order book in the next ``months`` months, and the open
planned orders of the latest MRP runs that should already have started.

Targets come from ``settings.CONTROL_TARGETS``. Status: a higher-is-better KPI is good at or above
its target, warning within 10 points below it, critical lower; inventory days are good at or below
the target, warning up to 1.5 times it, critical above; revenue at risk is good at zero, critical when
a HIGH-risk supplier is involved, warning otherwise. A KPI with nothing to measure has no value and
no status.

**Problems** are gathered from the modules that already detect them, each with an area, a severity,
what it is, the document it concerns and the role that owns it: cells down or offline (production),
MRP messages of the latest runs (planning), HIGH and MEDIUM supplier risks (purchasing), overdue
service tickets (service), late customer order lines and overdue supplier deliveries. Each problem
has a stable ``key`` and a recommendation (action, reason, expected impact); the owner's decision
about it is recorded by ``decisions`` (Wave 9b, ADR 0018). Nothing is stored here.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import Role
from apps.analytics.services import cost_of_sales_accounts
from apps.crm import services as crm
from apps.finance.models import JournalLine
from apps.inventory.models import MaterialDocumentItem
from apps.organization.models import Plant
from apps.planning import risk
from apps.planning.models import MRPMessage, MRPRun, PlannedOrder
from apps.planning.services import add_months, month_start
from apps.production.models import CellState
from apps.purchasing.models import GoodsReceipt, PurchaseOrder, PurchaseOrderItem
from apps.sales.models import Delivery, SalesOrder, SalesOrderItem

logger = logging.getLogger(__name__)

CENT, TENTH = Decimal("0.01"), Decimal("0.1")
CRITICAL, WARNING, GOOD = "critical", "warning", "good"

# Which roles see which area of the inbox: the same split as the API's read permissions.
AREA_ROLES = {
    "production": {Role.PRODUCTION_PLANNER, Role.INVENTORY_MANAGER, Role.FINANCE_MANAGER, Role.VIEWER},
    "planning": {Role.PRODUCTION_PLANNER, Role.INVENTORY_MANAGER, Role.PURCHASING_OFFICER, Role.FINANCE_MANAGER,
                 Role.VIEWER},
    "purchasing": {Role.PURCHASING_OFFICER, Role.INVENTORY_MANAGER, Role.FINANCE_MANAGER, Role.ACCOUNTANT,
                   Role.VIEWER},
    "sales": {Role.SALES_REP, Role.INVENTORY_MANAGER, Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.VIEWER},
    "service": {Role.SALES_REP, Role.FINANCE_MANAGER, Role.VIEWER},
}


def visible_areas(user) -> set[str]:
    """The inbox areas this user may see (administrators: all)."""
    if user.is_superuser or user.role == Role.ADMIN:
        return set(AREA_ROLES)
    return {area for area, roles in AREA_ROLES.items() if user.role in roles}


# --- KPIs ---------------------------------------------------------------------------------------------

def _percent(part: int, whole: int) -> Decimal | None:
    return (Decimal(part) * 100 / whole).quantize(TENTH) if whole else None


def _higher_is_better(value: Decimal | None, target: Decimal) -> str | None:
    if value is None:
        return None
    return GOOD if value >= target else WARNING if value >= target - 10 else CRITICAL


def otif_customers(company_code, start: date, end: date) -> dict:
    """Sales order lines due in [start, end) delivered in full by their requested date.

    Only lines with a requested delivery date are measured: without a promised date a delivery
    cannot be on time or late. Lines ordered in the window without one are counted separately
    (``without_date``) so the gap shows instead of being hidden (v0.19.1).
    """
    orders = (SalesOrderItem.objects.filter(order__company_code=company_code)
              .exclude(order__status=SalesOrder.Status.CANCELLED))
    lines = orders.filter(order__requested_delivery_date__gte=start,
                          order__requested_delivery_date__lt=end).select_related("order")
    due = on_time = 0
    for line in lines:
        delivered = line.delivery_items.filter(delivery__status=Delivery.Status.POSTED,
                                               delivery__delivery_date__lte=line.order.requested_delivery_date
                                               ).aggregate(q=Sum("quantity"))["q"]
        due += 1
        on_time += (delivered or 0) >= line.quantity
    without_date = orders.filter(order__requested_delivery_date__isnull=True, order__order_date__gte=start,
                                 order__order_date__lt=end).count()
    return {"lines": due, "on_time_in_full": on_time, "without_date": without_date,
            "value": _percent(on_time, due)}


def otif_suppliers(company_code, start: date, end: date) -> dict:
    """Purchase order lines due in [start, end) received in full by their delivery date."""
    lines = (PurchaseOrderItem.objects.filter(order__company_code=company_code, order__delivery_date__gte=start,
                                              order__delivery_date__lt=end)
             .exclude(order__status=PurchaseOrder.Status.CANCELLED).select_related("order"))
    due = on_time = 0
    for line in lines:
        received = line.receipt_items.filter(receipt__status=GoodsReceipt.Status.POSTED,
                                             receipt__receipt_date__lte=line.order.delivery_date
                                             ).aggregate(q=Sum("quantity"))["q"]
        due += 1
        on_time += (received or 0) >= line.quantity
    return {"lines": due, "on_time_in_full": on_time, "value": _percent(on_time, due)}


def forecast_accuracy(company_code, on: date) -> dict:
    """Accuracy over every plant's forecast materials, weighted by actual demand (8c)."""
    error = actual = Decimal("0")
    materials = 0
    for plant in Plant.objects.filter(company_code=company_code):
        for row in risk.forecast_accuracy(plant, on=on, months=6):
            materials += 1
            error += sum((abs(p["error"]) for p in row["points"]), Decimal("0"))
            actual += sum((p["actual"] for p in row["points"]), Decimal("0"))
    value = (max(Decimal("0"), 1 - error / actual) * 100).quantize(TENTH) if actual > 0 else None
    return {"materials": materials, "value": value}


def inventory_days(company_code, on: date, days: int) -> dict:
    """Stock value today against the average daily cost of sales of the last ``days`` days."""
    stock = MaterialDocumentItem.objects.filter(plant__company_code=company_code,
                                                document__posting_date__lte=on).aggregate(v=Sum("amount_local"))["v"]
    cogs = JournalLine.objects.filter(company_code=company_code, gl_account_id__in=cost_of_sales_accounts(company_code),
                                      posting_date__gt=on - timedelta(days=days), posting_date__lte=on
                                      ).aggregate(v=Sum("amount_local"))["v"]
    stock, cogs = Decimal(stock or 0).quantize(CENT), Decimal(cogs or 0).quantize(CENT)
    value = (stock / (cogs / days)).quantize(TENTH) if cogs > 0 else None
    return {"stock_value": stock, "cost_of_sales": cogs, "value": value}


def kpis(company_code, *, on: date, days: int = 90, months: int = 3) -> dict:
    """The north-star KPIs with targets and status, and the future-state figures beside them."""
    targets = {name: Decimal(str(value)) for name, value in settings.CONTROL_TARGETS.items()}
    start = on - timedelta(days=days)
    customers = otif_customers(company_code, start, on)
    suppliers = otif_suppliers(company_code, start, on)
    accuracy = forecast_accuracy(company_code, on)
    stock = inventory_days(company_code, on, days)
    suppliers_at_risk = risk.supplier_risk(company_code, on=on, months=months)
    at_risk = sum((r["revenue_at_risk"] for r in suppliers_at_risk), Decimal("0")).quantize(CENT)
    high = any(r["risk"] == "HIGH" for r in suppliers_at_risk)

    days_value, days_target = stock["value"], targets["inventory_days"]
    days_status = None if days_value is None else (
        GOOD if days_value <= days_target else WARNING if days_value <= days_target * Decimal("1.5") else CRITICAL)
    measures = [
        {"key": "otif_customers", "name": "On time in full, customers", "unit": "%", "value": customers["value"],
         "target": targets["otif_customers"], "status": _higher_is_better(customers["value"],
                                                                          targets["otif_customers"]),
         "basis": f"{customers['on_time_in_full']} of {customers['lines']} order lines due in the last {days} days"
                  + (f"; {customers['without_date']} line(s) without a requested date not measured"
                     if customers["without_date"] else "")},
        {"key": "otif_suppliers", "name": "On time in full, suppliers", "unit": "%", "value": suppliers["value"],
         "target": targets["otif_suppliers"], "status": _higher_is_better(suppliers["value"],
                                                                          targets["otif_suppliers"]),
         "basis": f"{suppliers['on_time_in_full']} of {suppliers['lines']} purchase lines due in the last {days} days"},
        {"key": "forecast_accuracy", "name": "Forecast accuracy", "unit": "%", "value": accuracy["value"],
         "target": targets["forecast_accuracy"], "status": _higher_is_better(accuracy["value"],
                                                                             targets["forecast_accuracy"]),
         "basis": f"{accuracy['materials']} forecast material(s), last 6 complete months"},
        {"key": "inventory_days", "name": "Inventory days", "unit": "days", "value": days_value,
         "target": days_target, "status": days_status,
         "basis": f"stock R{stock['stock_value']} against cost of sales R{stock['cost_of_sales']} in {days} days"},
        {"key": "revenue_at_risk", "name": "Revenue at risk", "unit": company_code.currency.code, "value": at_risk,
         "target": Decimal("0.00"), "status": GOOD if at_risk == 0 else CRITICAL if high else WARNING,
         "basis": f"open orders in the next {months} month(s) depending on a single-source supplier"},
    ]

    horizon_end = add_months(month_start(on), months)
    book = Decimal("0")
    for line in (SalesOrderItem.objects.filter(order__company_code=company_code,
                                               order__status__in=[SalesOrder.Status.OPEN, SalesOrder.Status.CONFIRMED])
                 .select_related("order")):
        wanted = line.order.requested_delivery_date or line.order.order_date
        if wanted < horizon_end:
            book += line.open_quantity * line.net_price
    late = PlannedOrder.objects.filter(run__in=_latest_runs(company_code), status=PlannedOrder.Status.OPEN,
                                       is_late=True).count()
    logger.info("Control-tower KPIs for %s on %s: %s", company_code.code, on,
                ", ".join(f"{m['key']}={m['value']}" for m in measures))
    return {"company_code": company_code.code, "on": on, "days": days, "months": months, "kpis": measures,
            "future": {"open_order_book": book.quantize(CENT), "late_planned_orders": late,
                       "suppliers_at_high_risk": sum(r["risk"] == "HIGH" for r in suppliers_at_risk)}}


def _latest_runs(company_code) -> list[int]:
    """The id of the newest MRP run per plant of the company code."""
    latest = {}
    for run_id, plant_id in (MRPRun.objects.filter(company_code=company_code)
                             .order_by("plant_id", "-planning_date", "-id").values_list("id", "plant_id")):
        latest.setdefault(plant_id, run_id)
    return list(latest.values())


# --- problems inbox, each with a recommendation (9a, 9b) --------------------------------------------------

def _advice(action: str, reason: str, kpi: str | None = None, value: Decimal | None = None,
            impact: str = "") -> dict:
    """A recommendation: what to do, why, and what it should move (Wave 9b).

    ``kpi`` is the control-tower KPI the action improves (or None), ``value`` the money involved where
    there is one (open sales or purchase value, revenue at risk), ``impact`` the same in words. The
    recommendation is a proposal; the owner decides (``decisions.decide``).
    """
    return {"action": action, "reason": reason,
            "impact": {"kpi": kpi, "value": None if value is None else Decimal(value).quantize(CENT), "text": impact}}


def _cell_advice(work_center, state: str) -> dict:
    """Recommendation for a cell that is down or offline: what waits on it, and where else it can run."""
    from apps.production.models import OrderOperation, ProductionOrder, WorkCenter

    waiting = sorted(set(OrderOperation.objects.filter(
        work_center=work_center, order__status__in=[ProductionOrder.Status.CREATED, ProductionOrder.Status.RELEASED])
        .values_list("order__order_number", flat=True)))
    alternatives = list(WorkCenter.objects.filter(line=work_center.line).exclude(pk=work_center.pk)
                        .exclude(live_state__state__in=[CellState.State.DOWN, CellState.State.OFFLINE])
                        .order_by("code").values_list("code", flat=True))
    action = ("Send maintenance to " + work_center.code + "." if state == CellState.State.DOWN
              else "Check the edge agent and network of " + work_center.code + ".")
    if waiting and alternatives:
        action += f" Move the waiting operations to {', '.join(alternatives)}."
    reason = (f"{len(waiting)} open production order(s) need {work_center.code}"
              + (f": {', '.join(waiting[:5])}" + (" ..." if len(waiting) > 5 else "") if waiting else "")
              + "." + ("" if alternatives else " No other cell on its line is available."))
    return _advice(action, reason, "otif_customers", None,
                   f"Protects delivery dates of {len(waiting)} production order(s)" if waiting
                   else "No production order waits on it yet")


def _mrp_advice(message) -> dict:
    """Recommendation for an MRP message: convert the late planned order it is about (v0.20.1).

    Names the planned order (run/number), what converting makes of it (a production order for MAKE;
    a contract release or purchase order for BUY), and the conversion endpoint. A planned order that
    was already converted points at its document instead. A message without a planned order (MAKE
    without a BOM) asks for the BOM.
    """
    planned, material = message.planned_order, message.material.number
    if planned is None:
        return _advice(f"Create and activate a BOM for {material} at plant {message.run.plant.code}; the next "
                       "MRP run (within 15 minutes) then plans its components.",
                       "Without a BOM, MRP cannot plan what the product is made of.", None, None,
                       "Components planned in time")
    label = f"{planned.run.run_number}/{planned.number}"
    what = f"{planned.quantity} x {material}, due {planned.due_date}"
    if planned.status == "CONVERTED":
        return _advice(f"Planned order {label} is already {planned.document_number}: expedite that order.",
                       f"It should have started on {planned.start_date}.", "otif_customers", None,
                       f"Supply of {what} in time")
    if planned.procurement == "BUY":
        action = (f"Convert planned order {label} ({what}) into a purchase order now (a release if a contract "
                  "covers it) and ask the supplier to expedite; or move the demand it covers.")
    else:
        action = (f"Convert planned order {label} ({what}) into a production order and release it today; "
                  "or move the demand it covers.")
    action += f" API: POST /api/planning/planned-orders/{planned.pk}/convert/"
    late = (message.run.planning_date - planned.start_date).days
    return _advice(action, f"It should have started on {planned.start_date} ({late} day(s) before the MRP run); "
                           "every day it waits, the demand it covers is served later.", "otif_customers", None,
                   f"Supply of {what} in time for its demand")


def _late_order_advice(line, open_quantity: Decimal) -> dict:
    """Recommendation for a late customer order line: deliver from stock if there is enough, else re-promise."""
    from apps.inventory.services import plant_stock

    in_stock, _value = plant_stock(line.material, line.plant)
    value = open_quantity * line.net_price
    if in_stock >= open_quantity:
        action = f"Deliver now: {in_stock} {line.material.number} in stock at plant {line.plant.code}."
        reason = "The stock to complete the line is already there."
    else:
        action = (f"Agree a new date with {line.order.customer.name} and expedite supply "
                  f"({in_stock} of {open_quantity} in stock).")
        reason = "There is not enough stock to complete the line today."
    return _advice(action, reason, "otif_customers", value, f"R{value.quantize(CENT)} of open sales delivered")


def problems(company_code, *, on: date, now=None, areas: set[str] | None = None) -> list[dict]:
    """Everything that needs a person's attention, critical first, then the oldest first.

    Each problem carries a ``recommendation`` (action, reason, expected impact, Wave 9b) and the role
    that owns it (``owner``, label; ``owner_role``, code), who may accept or reject it.
    """
    now = now or timezone.now()
    areas = set(AREA_ROLES) if areas is None else areas
    found: list[dict] = []

    def add(area, severity, key, title, detail, document, owner, since, advice):
        """Record one problem, if its area is one the caller may see. ``advice`` is called only then."""
        if area in areas:
            found.append({"key": key, "area": area, "severity": severity, "title": title, "detail": detail,
                          "document": document, "owner": owner.label, "owner_role": owner.value, "since": since,
                          "recommendation": advice()})

    for cell in (CellState.objects.filter(work_center__plant__company_code=company_code,
                                          state__in=[CellState.State.DOWN, CellState.State.OFFLINE])
                 .select_related("work_center__plant", "work_center__line")):
        wc = cell.work_center
        started = cell.since or cell.reported_at
        # The start time is part of the key: the same cell failing again later is a new problem (9b).
        add("production", CRITICAL, f"cell:{wc.plant.code}/{wc.code}:{cell.state}:{started:%Y%m%dT%H%M}",
            f"Cell {wc.code} is {cell.state.lower()}", f"Plant {wc.plant.code}, since {started}",
            wc.code, Role.PRODUCTION_PLANNER, started.date(), lambda wc=wc, state=cell.state: _cell_advice(wc, state))

    for message in (MRPMessage.objects.filter(run__in=_latest_runs(company_code))
                    .select_related("run__plant", "material", "planned_order")):
        planned = message.planned_order
        # A BUY order is converted by purchasing, a MAKE order (and a missing BOM) by the planner (8b).
        owner = Role.PURCHASING_OFFICER if planned and planned.procurement == "BUY" else Role.PRODUCTION_PLANNER
        add("planning", WARNING, f"mrp:{message.run.run_number}:{message.pk}",
            f"MRP: {message.material.number}", message.text, message.run.run_number, owner,
            message.run.planning_date, lambda m=message: _mrp_advice(m))

    for row in risk.supplier_risk(company_code, on=on):
        if row["risk"] in ("HIGH", "MEDIUM"):
            add("purchasing", CRITICAL if row["risk"] == "HIGH" else WARNING,
                f"supplier-risk:{row['supplier']}:{row['risk']}",
                f"{row['risk'].title()} supply risk: {row['name']}",
                f"R{row['revenue_at_risk']} of open orders at risk. {row['recommendation']}",
                row["supplier"], Role.PURCHASING_OFFICER, on,
                lambda r=row: _advice(r["recommendation"],
                                      f"Only {r['name']} supplies "
                                      f"{', '.join(m['material'] for m in r['materials'] if m['single_source'])}; "
                                      f"orders {', '.join(r['orders_at_risk'][:5]) or '-'} depend on it.",
                                      "revenue_at_risk", r["revenue_at_risk"],
                                      f"R{r['revenue_at_risk']} of open sales no longer depends on one supplier"))

    for ticket in crm.overdue_tickets(company_code, now):
        add("service", CRITICAL if ticket.priority in ("URGENT", "HIGH") else WARNING,
            f"ticket:{ticket.ticket_number}", f"Overdue ticket {ticket.ticket_number} ({ticket.priority.lower()})",
            f"{ticket.subject} - {ticket.customer.name}, due {timezone.localtime(ticket.due_at):%Y-%m-%d %H:%M}",
            ticket.ticket_number, Role.SALES_REP, timezone.localtime(ticket.due_at).date(),
            lambda t=ticket: _advice(f"Call {t.customer.name} today and give the ticket an owner.",
                                     f"The {t.priority.lower()} ticket is past its response time.", None, None,
                                     "Customer kept informed; service level restored"))

    for line in (SalesOrderItem.objects.filter(order__company_code=company_code,
                                               order__status__in=[SalesOrder.Status.OPEN, SalesOrder.Status.CONFIRMED])
                 .select_related("order__customer", "material", "plant")):
        due = line.order.requested_delivery_date or line.order.order_date
        open_quantity = line.open_quantity
        if due < on and open_quantity > 0:
            late = (on - due).days
            add("sales", CRITICAL if late > 7 else WARNING, f"late-order:{line.order.order_number}/{line.item_number}",
                f"Late delivery {line.order.order_number}/{line.item_number}",
                f"{open_quantity} x {line.material.number} for {line.order.customer.name}, due {due} "
                f"({late} day(s) late)",
                line.order.order_number, Role.SALES_REP, due,
                lambda ln=line, q=open_quantity: _late_order_advice(ln, q))

    for line in (PurchaseOrderItem.objects.filter(order__company_code=company_code, order__delivery_date__lt=on,
                                                  order__status__in=[PurchaseOrder.Status.OPEN,
                                                                     PurchaseOrder.Status.RELEASED])
                 .select_related("order__supplier", "material")):
        open_quantity = line.quantity - line.received_quantity
        if open_quantity > 0:
            late = (on - line.order.delivery_date).days
            value = open_quantity * line.net_price
            add("purchasing", CRITICAL if late > 7 else WARNING,
                f"late-receipt:{line.order.order_number}/{line.item_number}",
                f"Overdue delivery {line.order.order_number}/{line.item_number}",
                f"{open_quantity} x {line.material.number} from {line.order.supplier.name}, due "
                f"{line.order.delivery_date} ({late} day(s) late)",
                line.order.order_number, Role.PURCHASING_OFFICER, line.order.delivery_date,
                lambda ln=line, q=open_quantity, v=value: _advice(
                    f"Ask {ln.order.supplier.name} for a confirmed date for {q} x {ln.material.number}; "
                    "if none within 2 working days, buy the shortfall elsewhere.",
                    "The goods are overdue and what depends on them waits.", "otif_suppliers", v,
                    f"R{v.quantize(CENT)} of purchases received"))

    found.sort(key=lambda p: (p["severity"] != CRITICAL, p["since"], p["key"]))
    logger.info("Problems inbox for %s on %s: %d problem(s), %d critical", company_code.code, on, len(found),
                sum(p["severity"] == CRITICAL for p in found))
    return found
