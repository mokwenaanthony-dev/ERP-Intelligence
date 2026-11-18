"""MRP (Wave 8b, ADR 0015): what to make and buy, and when, so that demand is met from stock.

In three steps, the classic MRP II sequence (SAP MD01/MD04):

1. **Snapshot** (``snapshot``): read everything the plan depends on into plain data: planning
   parameters, stock, independent demand (forecast and open sales orders), scheduled receipts (open
   purchase and production orders), dependent demand of open production orders, and BOMs. The
   snapshot is fingerprinted (SHA-256): **running MRP again on unchanged data returns the stored run**
   (idempotent, ADR 0010).
2. **Plan** (``plan``): a pure function over the snapshot, testable without a database. Materials
   in low-level-code order (a finished product before the components it uses); each material's
   dated events in date order (receipts first on the same day)::

       available = available + receipt   or   available - requirement
       if available < safety stock: a planned order for the shortage, sized by the lot-size rule,
                                    due that day, started lead time earlier;
                                    for MAKE, its components become requirements on the start date

   Orders are therefore due on the day they are needed (not at the start of a month); the monthly
   requirements list (SAP MD04) sums the events per month for reading.

3. **Store** (``run_mrp``): one numbered run (MR) with the requirements list, planned orders and
   messages; the open planned orders of earlier runs are superseded (MRP is regenerative). People
   then **convert** planned orders (``convert``): a MAKE order becomes a production order through
   ``apps.production.orders``; a BUY order becomes a release against an active SRM contract when
   one covers it, otherwise a purchase order through ``apps.purchasing``. Converting twice returns
   the same document.

**Independent demand** is the open sales orders on their dates, plus the part of each month's
forecast that the month's orders do not cover, at the start of the month (orders "consume" the
forecast rather than add to it). In the current month, deliveries already posted are taken off the
forecast. Anything already overdue counts on the planning date.

Not modelled (yet): capacity, BOM versions changing inside the horizon (the BOM valid on the planning
date is used), scrap rates, working-day calendars (lead times are calendar days).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce

from apps.finance import services as finance
from apps.inventory import services as inventory
from apps.inventory.models import MaterialDocumentItem, MovementType
from apps.masterdata.models import BusinessPartner, Material
from apps.production import orders as production_orders
from apps.production import services as production
from apps.production.models import ProductionOrder
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem
from apps.sales.models import SalesOrder, SalesOrderItem
from apps.srm import services as srm
from apps.srm.models import Contract

from .models import (ForecastPeriod, MaterialPlanning, MRPMaterial, MRPMessage, MRPRun, PlannedOrder,
                     PlanningLine)
from .services import ForecastError, add_months, month_start

logger = logging.getLogger(__name__)

ZERO = Decimal("0.000")
THOUSANDTH = Decimal("0.001")
MAKE, BUY = MaterialPlanning.Procurement.MAKE, MaterialPlanning.Procurement.BUY
DEFAULT_LEAD_DAYS = {MAKE: 5, BUY: 14}
MAX_HORIZON = 12
MAX_LEVELS = 20  # the BOM rules already refuse cycles; this guards the low-level codes


class MRPError(ForecastError):
    """MRP or a conversion breaks a planning rule (a 400 in the API)."""


# --- 1. the snapshot ----------------------------------------------------------------------------------

def _q(value) -> Decimal:
    return (Decimal(value) if value is not None else ZERO).quantize(THOUSANDTH)


def _when(day: date | None, planning_date: date, end: date) -> date | None:
    """The date a requirement or receipt counts on: past dates count today, dates after the horizon not at all."""
    if day is None:
        return None
    day = max(day, planning_date)
    return day if day < end else None


def snapshot(plant, planning_date: date, horizon: int) -> dict:
    """Everything the plan for ``plant`` depends on, as plain JSON-ready data (quantities as strings).

    Shape: ``{"plant", "planning_date", "months", "materials": {number: {...}}, "boms": {number:
    {"base": q, "items": [[component, q], ...]}}}``. Each material has its parameters, its stock, and
    dated events ``[[YYYY-MM-DD, quantity], ...]``: ``independent`` (sales orders on their dates; the
    forecast that orders do not cover, at the start of its month), ``receipts`` (open purchase and
    production orders) and ``dependent`` (components of open production orders, on their start date).
    Dates in the past count on the planning date; dates after the horizon are left out.
    """
    first = month_start(planning_date)
    months = [add_months(first, i) for i in range(horizon)]
    end = add_months(first, horizon)
    materials: dict[str, dict] = {}

    def entry(material) -> dict:
        """The material's record in the snapshot, created on first use."""
        if material.number not in materials:
            materials[material.number] = {"independent": {}, "receipts": {}, "dependent": {}, "_obj": material}
        return materials[material.number]

    def add(material, kind, day, quantity):
        """Add a dated event of ``kind``; past dates count today, dates after the horizon are dropped."""
        day = _when(day, planning_date, end)
        if day is None or quantity <= 0:
            return
        events = entry(material)[kind]
        events[day] = events.get(day, ZERO) + quantity

    # Independent demand: open sales order items at the plant, on their requested (or order) date.
    so_items = (SalesOrderItem.objects.filter(plant=plant, order__status__in=[SalesOrder.Status.OPEN,
                                                                              SalesOrder.Status.CONFIRMED])
                .select_related("order", "material"))
    ordered: dict[str, dict[date, Decimal]] = {}  # per material and month, to consume the forecast
    for item in so_items:
        open_quantity = item.open_quantity
        day = _when(item.order.requested_delivery_date or item.order.order_date, planning_date, end)
        if open_quantity > 0 and day is not None:
            add(item.material, "independent", day, open_quantity)
            per = ordered.setdefault(item.material.number, {})
            per[month_start(day)] = per.get(month_start(day), ZERO) + open_quantity

    # The forecast: the latest forecast run at the plant that includes each material. Orders consume it:
    # only the part of a month's forecast that its orders do not cover is added, at the month's start.
    periods = (ForecastPeriod.objects.filter(item__run__plant=plant, period__gte=first, period__lt=end)
               .select_related("item__material", "item__run")
               .order_by("item__material__number", "-item__run__first_period", "-item__run__id", "period"))
    delivered = dict(MaterialDocumentItem.objects.filter(
        plant=plant, document__movement_type=MovementType.GOODS_ISSUE_DELIVERY)
        .annotate(day=Coalesce("document__reversal_of__posting_date", "document__posting_date"))
        .filter(day__gte=first, day__lte=planning_date)
        .values_list("material__number").annotate(q=Sum("quantity")).order_by())
    chosen_run: dict[int, int] = {}
    for period in periods:
        material = period.item.material
        if chosen_run.setdefault(material.pk, period.item.run_id) != period.item.run_id:
            continue  # an older run's forecast for this material
        forecast = period.quantity
        if period.period == first:
            # This month's deliveries so far already left stock (issues are negative quantities).
            forecast += delivered.get(material.number) or ZERO
        uncovered = forecast - ordered.get(material.number, {}).get(period.period, ZERO)
        add(material, "independent", period.period, uncovered)

    # Scheduled receipts: open purchase order items (ordered - received) on their delivery date.
    po_items = (PurchaseOrderItem.objects.filter(plant=plant, order__status__in=[PurchaseOrder.Status.OPEN,
                                                                                 PurchaseOrder.Status.RELEASED])
                .select_related("order", "material"))
    for item in po_items:
        add(item.material, "receipts", item.order.delivery_date or item.order.order_date,
            item.quantity - item.received_quantity)
    # ... and open production orders (ordered - received) on their finish date; their components not
    # yet issued are dependent demand on the start date.
    for order in (ProductionOrder.objects.filter(plant=plant, status__in=[ProductionOrder.Status.CREATED,
                                                                          ProductionOrder.Status.RELEASED])
                  .select_related("material").prefetch_related("components__material")):
        add(order.material, "receipts", order.finish_date, order.quantity - production_orders.delivered_quantity(order))
        issued = production_orders.issued_quantities(order)
        for component in order.components.all():
            add(component.material, "dependent", order.start_date,
                component.required_quantity - issued.get(component.material_id, ZERO))

    # Materials with planning parameters are planned even without demand (safety stock).
    parameters = {p.material.number: p for p in MaterialPlanning.objects.filter(plant=plant)
                  .select_related("material", "preferred_supplier")}
    for p in parameters.values():
        entry(p.material)

    # BOMs: explode MAKE materials down to the leaves, so components enter the plan.
    boms: dict[str, dict] = {}
    queue = list(materials.values())
    while queue:
        data = queue.pop()
        material = data["_obj"]
        if "procurement" in data:
            continue
        bom = production.find_bom(material, plant, planning_date)
        p = parameters.get(material.number)
        procurement = p.procurement if p else (MAKE if bom else BUY)
        data.update({
            "procurement": procurement,
            "lot_size": p.lot_size if p else MaterialPlanning.LotSize.LOT_FOR_LOT,
            "lot_quantity": str(_q(p.lot_quantity)) if p and p.lot_quantity else None,
            "safety_stock": str(_q(p.safety_stock if p else ZERO)),
            "lead_time_days": p.lead_time_days if p else DEFAULT_LEAD_DAYS[procurement],
            "stock": str(inventory.plant_stock(material, plant)[0].quantize(THOUSANDTH)),
            "has_bom": bom is not None,
        })
        if procurement == MAKE and bom is not None:
            items = list(bom.items.select_related("component").order_by("item_number"))
            boms[material.number] = {"base": str(_q(bom.base_quantity)),
                                     "items": [[i.component.number, str(_q(i.quantity))] for i in items]}
            for item in items:
                queue.append(entry(item.component))

    def events(values: dict) -> list:
        """Dated quantities as ``[[YYYY-MM-DD, quantity], ...]`` in date order (stable for the fingerprint)."""
        return [[day.isoformat(), str(_q(quantity))] for day, quantity in sorted(values.items())]

    return {
        "plant": plant.code, "planning_date": planning_date.isoformat(), "months": [f"{m:%Y-%m}" for m in months],
        "materials": {number: {k: (events(v) if isinstance(v, dict) else v) for k, v in sorted(data.items())
                               if k != "_obj"}
                      for number, data in sorted(materials.items())},
        "boms": dict(sorted(boms.items())),
    }


def fingerprint(snap: dict) -> str:
    """SHA-256 of the snapshot in a canonical form: equal snapshots, equal fingerprints."""
    return hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- 2. the plan (pure) -------------------------------------------------------------------------------

@dataclass
class Proposal:
    """A planned order the plan proposes."""

    material: str
    procurement: str
    quantity: Decimal
    start_date: date
    due_date: date
    is_late: bool


@dataclass
class Plan:
    """The result of ``plan``: per material its level and monthly lines, the proposals, the messages."""

    levels: dict[str, int] = field(default_factory=dict)
    lines: dict[str, list[dict]] = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)
    # Message index -> number (1-based) of the proposal it is about, so the control tower can name the
    # planned order and propose converting it (v0.20.1).
    message_orders: dict[int, int] = field(default_factory=dict)


def low_level_codes(materials: list[str], boms: dict) -> dict[str, int]:
    """The deepest level each material appears at in any BOM (0 if it is in none): plan in this order."""
    codes = {m: 0 for m in materials}
    for _ in range(MAX_LEVELS):
        changed = False
        for parent, bom in boms.items():
            for component, _quantity in bom["items"]:
                if codes.get(component, 0) < codes.get(parent, 0) + 1:
                    codes[component] = codes.get(parent, 0) + 1
                    changed = True
        if not changed:
            return codes
    raise MRPError(f"The BOMs are deeper than {MAX_LEVELS} levels.")


def lot(shortage: Decimal, rule: str, lot_quantity: Decimal | None) -> Decimal:
    """The order quantity for a shortage: exactly it, whole fixed lots, or at least the minimum lot."""
    if rule == MaterialPlanning.LotSize.FIXED:
        return (lot_quantity * math.ceil(shortage / lot_quantity)).quantize(THOUSANDTH)
    if rule == MaterialPlanning.LotSize.MINIMUM:
        return max(shortage, lot_quantity).quantize(THOUSANDTH)
    return shortage.quantize(THOUSANDTH, rounding=ROUND_CEILING)


RECEIPT, INDEPENDENT, DEPENDENT = "receipts", "independent", "dependent"


def plan(snap: dict) -> Plan:
    """Net requirements and planned orders for the snapshot. Deterministic; reads no database.

    Each material's events are processed in date order (receipts before requirements on the same
    day). Whenever the projected stock falls below the safety stock, a planned order covers the
    shortage, due that day (the planning date if the stock is short already); for MAKE, its
    components become dependent demand on the order's start date. The monthly lines sum the events.
    """
    planning_date = date.fromisoformat(snap["planning_date"])
    months = [date.fromisoformat(f"{m}-01") for m in snap["months"]]
    materials = snap["materials"]
    result = Plan(levels=low_level_codes(list(materials), snap["boms"]))
    added: dict[str, list[tuple[date, Decimal]]] = {m: [] for m in materials}  # dependent demand from planning

    for number in sorted(materials, key=lambda m: (result.levels[m], m)):
        data = materials[number]
        safety = Decimal(data["safety_stock"])
        lot_quantity = Decimal(data["lot_quantity"]) if data["lot_quantity"] else None
        available = Decimal(data["stock"])
        if data["procurement"] == MAKE and not data["has_bom"]:
            result.messages.append((number, "Planned as MAKE but has no active BOM at the plant: components are "
                                            "not planned."))
        events = [(date.fromisoformat(day), 0, RECEIPT, Decimal(q)) for day, q in data[RECEIPT]]
        events += [(date.fromisoformat(day), 1, INDEPENDENT, Decimal(q)) for day, q in data[INDEPENDENT]]
        events += [(date.fromisoformat(day), 1, DEPENDENT, Decimal(q)) for day, q in data[DEPENDENT]]
        events += [(day, 1, DEPENDENT, q) for day, q in added.get(number, [])]
        totals = {m: {RECEIPT: ZERO, INDEPENDENT: ZERO, DEPENDENT: ZERO, "planned": ZERO} for m in months}
        month_end: dict[date, Decimal] = {}

        def order(due: date, shortage: Decimal) -> Decimal:
            """Propose a planned order for the shortage due on ``due``; explode MAKE orders; return its quantity."""
            quantity = lot(shortage, data["lot_size"], lot_quantity)
            start = due - timedelta(days=data["lead_time_days"])
            late = start < planning_date
            result.proposals.append(Proposal(number, data["procurement"], quantity, start, due, late))
            if late:
                result.message_orders[len(result.messages)] = len(result.proposals)
                result.messages.append((number, f"Planned order due {due} should have started on {start}: "
                                                f"expedite or move the demand."))
            totals[month_start(due)]["planned"] += quantity
            bom = snap["boms"].get(number)
            if data["procurement"] == MAKE and bom:
                for component, per_base in bom["items"]:
                    need = (Decimal(per_base) * quantity / Decimal(bom["base"])).quantize(THOUSANDTH)
                    added.setdefault(component, []).append((max(start, planning_date), need))
            return quantity

        if available < safety:  # short already: restore the safety stock now
            available += order(planning_date, safety - available)
        carried = available  # projected stock at the start of the horizon
        for day, _priority, kind, quantity in sorted(events, key=lambda e: (e[0], e[1], e[2])):
            totals[month_start(day)][kind] += quantity
            available += quantity if kind == RECEIPT else -quantity
            if available < safety:
                available += order(day, safety - available)
            month_end[month_start(day)] = available
        lines = []
        for month in months:
            carried = month_end.get(month, carried)  # a month without events ends where the last one did
            lines.append({"period": month, "independent": totals[month][INDEPENDENT].quantize(THOUSANDTH),
                          "dependent": totals[month][DEPENDENT].quantize(THOUSANDTH),
                          "receipts": totals[month][RECEIPT].quantize(THOUSANDTH),
                          "planned": totals[month]["planned"].quantize(THOUSANDTH),
                          "available": carried.quantize(THOUSANDTH)})
        result.lines[number] = lines
    return result


# --- 3. store, and convert ------------------------------------------------------------------------------

def run_mrp(plant, *, planning_date: date, horizon: int = 6, user=None) -> tuple[MRPRun, bool]:
    """Plan ``plant`` from ``planning_date`` for ``horizon`` months. Returns ``(run, created)``.

    Unchanged data on the same planning date returns the stored run (``created`` False). A new run
    supersedes the open planned orders of the plant's earlier runs.
    """
    if not 1 <= horizon <= MAX_HORIZON:
        raise MRPError(f"The MRP horizon must be 1 to {MAX_HORIZON} months.")
    snap = snapshot(plant, planning_date, horizon)
    if not snap["materials"]:
        raise MRPError(f"Plant {plant.code} has no demand, receipts or planning parameters: nothing to plan.")
    digest = fingerprint(snap)
    existing = MRPRun.objects.filter(plant=plant, fingerprint=digest).first()
    if existing:
        logger.info("MRP run %s already planned this situation for plant %s: returned unchanged",
                    existing.run_number, plant.code)
        return existing, False
    result = plan(snap)
    run = _store(plant, planning_date, horizon, snap, result, digest, user)
    logger.info("MRP run %s for plant %s on %s: %d material(s), %d planned order(s) (%d late), %d message(s)",
                run.run_number, plant.code, planning_date, len(result.lines), len(result.proposals),
                sum(p.is_late for p in result.proposals), len(result.messages))
    return run, True


@transaction.atomic
def _store(plant, planning_date, horizon, snap, result: Plan, digest, user) -> MRPRun:
    company = plant.company_code
    # Lock the plant's runs so two MRP runs for one plant cannot interleave their supersede and create.
    list(MRPRun.objects.select_for_update().filter(plant=plant).values_list("pk", flat=True))
    superseded = PlannedOrder.objects.filter(run__plant=plant, status=PlannedOrder.Status.OPEN)
    count = superseded.update(status=PlannedOrder.Status.SUPERSEDED)  # no signals needed: not projected
    run = MRPRun.objects.create(company_code=company, plant=plant,
                                run_number=finance.next_document_number(company, 0, "MR"),
                                planning_date=planning_date, first_period=month_start(planning_date),
                                horizon=horizon, fingerprint=digest, created_by=user, updated_by=user)
    numbers = sorted(result.lines, key=lambda m: (result.levels[m], m))
    by_number = {m.number: m for m in Material.objects.filter(number__in=numbers)}
    for number in numbers:
        data = snap["materials"][number]
        item = MRPMaterial.objects.create(run=run, material=by_number[number], low_level_code=result.levels[number],
                                          procurement=data["procurement"], opening_stock=Decimal(data["stock"]),
                                          safety_stock=Decimal(data["safety_stock"]))
        PlanningLine.objects.bulk_create([PlanningLine(item=item, **line) for line in result.lines[number]])
    PlannedOrder.objects.bulk_create([
        PlannedOrder(run=run, number=index, material=by_number[p.material], procurement=p.procurement,
                     quantity=p.quantity, start_date=p.start_date, due_date=p.due_date, is_late=p.is_late)
        for index, p in enumerate(result.proposals, start=1)])
    planned = {p.number: p for p in run.planned_orders.all()}
    MRPMessage.objects.bulk_create([MRPMessage(run=run, material=by_number[m], text=text[:200],
                                               planned_order=planned.get(result.message_orders.get(index)))
                                    for index, (m, text) in enumerate(result.messages)])
    if count:
        logger.info("Superseded %d open planned order(s) of earlier MRP runs for plant %s", count, plant.code)
    return run


def _contract_for(planned: PlannedOrder, on: date):
    """An active contract valid on ``on`` that still covers the planned quantity, cheapest first."""
    candidates = (Contract.objects.filter(company_code=planned.run.company_code, status=Contract.Status.ACTIVE,
                                          valid_from__lte=on, valid_to__gte=on, items__material=planned.material)
                  .select_related("supplier", "purchasing_organization")
                  .order_by("items__net_price", "contract_number"))
    for contract in candidates:
        target = contract.items.get(material=planned.material).target_quantity
        if target - srm.released_quantities(contract).get(planned.material_id, ZERO) >= planned.quantity:
            return contract
    return None


def _last_purchase(material, plant, supplier=None):
    items = PurchaseOrderItem.objects.filter(material=material, plant=plant).exclude(
        order__status=PurchaseOrder.Status.CANCELLED)
    if supplier is not None:
        items = items.filter(order__supplier=supplier)
    return items.select_related("order__supplier", "order__purchasing_organization").order_by(
        "-order__order_date", "-order__id").first()


@transaction.atomic
def convert(planned: PlannedOrder, *, on: date, supplier: BusinessPartner | None = None,
            net_price: Decimal | None = None, user=None) -> str:
    """Turn a planned order into a production order (MAKE) or a purchase order (BUY); returns its number.

    BUY uses, in this order: an active contract that still covers the quantity (a release at the
    contract price), the supplier given, the material's preferred supplier, the supplier of the last
    purchase order. The price is the one given, else that supplier's last price. Idempotent: a
    converted planned order returns its document again.
    """
    locked = PlannedOrder.objects.select_for_update().select_related("run__plant", "run__company_code", "material")
    planned = locked.get(pk=planned.pk)
    if planned.status == PlannedOrder.Status.CONVERTED:
        return planned.document_number
    if planned.status != PlannedOrder.Status.OPEN:
        raise MRPError(f"Planned order {planned.run.run_number}/{planned.number} is {planned.status}: convert the "
                       f"planned orders of the latest MRP run.")
    plant = planned.run.plant
    if planned.procurement == MAKE:
        order = production_orders.create_order(plant=plant, material=planned.material, quantity=planned.quantity,
                                               start_date=max(planned.start_date, on),
                                               finish_date=max(planned.due_date, planned.start_date, on), user=user)
        number = order.order_number
    else:
        contract = None if supplier or net_price else _contract_for(planned, on)
        if contract is not None:
            order = srm.release_order(contract, order_date=on, items=[(planned.material, planned.quantity)],
                                      delivery_date=max(planned.due_date, on), user=user)
        else:
            parameters = MaterialPlanning.objects.filter(material=planned.material, plant=plant).first()
            last = _last_purchase(planned.material, plant)
            supplier = supplier or (parameters.preferred_supplier if parameters else None) or (
                last.order.supplier if last else None)
            if supplier is None:
                raise MRPError(f"No contract, preferred supplier or earlier purchase order for "
                               f"{planned.material.number}: name the supplier and the price.")
            if net_price is None:
                last_from_supplier = _last_purchase(planned.material, plant, supplier)
                if last_from_supplier is None:
                    raise MRPError(f"No earlier price from supplier {supplier.number} for {planned.material.number}: "
                                   f"give the net price.")
                net_price = last_from_supplier.net_price
            organization = (last.order.purchasing_organization if last else
                            planned.run.company_code.purchasing_organizations.order_by("code").first())
            order = purchasing.create_purchase_order(
                purchasing_organization=organization, supplier=supplier, order_date=on,
                delivery_date=max(planned.due_date, on), user=user,
                items=[purchasing.OrderItemInput(material=planned.material, quantity=planned.quantity,
                                                 net_price=net_price, plant=plant)])
        number = order.order_number
    planned.status, planned.document_number = PlannedOrder.Status.CONVERTED, number
    planned.save(update_fields=["status", "document_number"])
    logger.info("Planned order %s/%d (%s %s x %s) converted into %s", planned.run.run_number, planned.number,
                planned.procurement, planned.material.number, planned.quantity, number)
    return number
