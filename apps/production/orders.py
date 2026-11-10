"""Production orders: create, release, issue components, confirm work, receive output, settle.

The order is a cost object of the Universal Journal: every posting below carries its order number
(``JournalLine.order_number``), so its actual cost is a sum over journal lines like any other report.

    Issue components (261)    Dr consumption 5400 (order)       Cr inventory
    Confirm an operation      Dr allocation 6950 (order)        Cr allocation 6950 (work centre's cost centre)
    Receive the product (101) Dr inventory, at planned unit cost Cr factory output 5500 (order)
    Complete and settle       Dr/Cr production variances 5600   Cr/Dr factory output 5500 (order)

After settlement the order balance is zero: the product went into stock at its planned cost, the cost
centre was relieved of the work it did, and any difference between actual and planned cost is a
production variance in the income statement. The settlement entry is the variance (positive =
actual cost above plan).

Not yet: work in process at month end for orders that stay open, reversing confirmations, and
by-products. Goods movements of an order can be reversed through the stock ledger as usual.
"""
import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from apps.finance import services as finance
from apps.finance.models import DocumentType, JournalLine
from apps.finance.services import LineInput
from apps.inventory import services as inventory
from apps.inventory.models import AccountKey, MaterialDocumentItem, MovementType

from . import services
from .models import (
    Confirmation,
    ConfirmationActivity,
    OrderComponent,
    OrderOperation,
    OrderOperationActivity,
    ProductionOrder,
)
from .services import ProductionError

logger = logging.getLogger(__name__)

CONTINUOUS = 0  # order numbers run continuously, like sales and purchasing documents
CENT = Decimal("0.01")
QUANTITY = Decimal("0.001")
ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _quantity(value) -> Decimal:
    return Decimal(value).quantize(QUANTITY, rounding=ROUND_HALF_UP)


def _lock(order) -> ProductionOrder:
    """Re-read the order with a row lock, so two postings to one order are serialised."""
    return ProductionOrder.objects.select_for_update().select_related(
        "company_code", "plant__company_code", "material", "receipt_storage_location"
    ).get(pk=order.pk)


def _require_released(order):
    if order.status != ProductionOrder.Status.RELEASED:
        raise ProductionError(f"Order {order.order_number} is {order.get_status_display().lower()}; "
                              "only released orders can be posted to.")


# --- create, release, cancel -----------------------------------------------------------------

@transaction.atomic
def create_order(*, plant, material, quantity: Decimal, start_date: date, finish_date: date | None = None,
                 receipt_storage_location=None, user=None) -> ProductionOrder:
    """Create an order from the BOM and routing valid on ``start_date``, with its planned cost."""
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ProductionError("Order quantity must be greater than zero.")
    finish_date = finish_date or start_date
    if finish_date < start_date:
        raise ProductionError("The finish date cannot be before the start date.")
    bom = services.find_bom(material, plant, start_date)
    if bom is None:
        raise ProductionError(f"No active BOM for {material.number} in plant {plant.code} on {start_date}.")
    routing = services.find_routing(material, plant, start_date)
    # The estimate also checks every component can be priced and every activity has a rate.
    estimate = services.cost_estimate(material, plant, quantity, start_date)
    if receipt_storage_location is None:
        receipt_storage_location = plant.storage_locations.order_by("code").first()
    if receipt_storage_location is None or receipt_storage_location.plant_id != plant.pk:
        raise ProductionError(f"Give a storage location of plant {plant.code} for the finished goods.")

    company_code = plant.company_code
    order = ProductionOrder.objects.create(
        company_code=company_code, plant=plant,
        order_number=finance.next_document_number(company_code, CONTINUOUS, "MO"),
        material=material, quantity=quantity, unit=material.base_unit,
        receipt_storage_location=receipt_storage_location, start_date=start_date, finish_date=finish_date,
        bom=bom, routing=routing, planned_cost=estimate["total"],
        planned_unit_cost=(estimate["total"] / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        created_by=user, updated_by=user,
    )
    planned_materials = {row["item_number"]: row["value"] for row in estimate["materials"]}
    OrderComponent.objects.bulk_create([
        OrderComponent(order=order, item_number=item.item_number, material=item.component,
                       required_quantity=_quantity(item.quantity * quantity / bom.base_quantity),
                       planned_value=planned_materials[item.item_number], storage_location=receipt_storage_location)
        for item in bom.items.select_related("component").order_by("item_number")
    ])
    if routing is not None:
        planned_activities = {(row["operation_number"], row["activity_type"]): row["value"]
                              for row in estimate["activities"]}
        for op in routing.operations.select_related("work_center").order_by("operation_number"):
            operation = OrderOperation.objects.create(order=order, operation_number=op.operation_number,
                                                      description=op.description, work_center=op.work_center)
            OrderOperationActivity.objects.bulk_create([
                OrderOperationActivity(
                    operation=operation, activity_type=act.activity_type,
                    planned_quantity=services.planned_activity(act, quantity, routing.base_quantity),
                    planned_value=planned_activities[(op.operation_number, act.activity_type.code)],
                )
                for act in op.activities.select_related("activity_type")
            ])
    logger.info("Created production order %s: %s x %s in plant %s, planned cost %s", order.order_number,
                quantity, material.number, plant.code, order.planned_cost)
    return order


@transaction.atomic
def release_order(order, release_date: date, user=None) -> ProductionOrder:
    """Release a created order for production: from now on it can be posted to."""
    order = _lock(order)
    if order.status != ProductionOrder.Status.CREATED:
        raise ProductionError(f"Order {order.order_number} is {order.get_status_display().lower()}, not created.")
    order.status, order.released_at, order.updated_by = ProductionOrder.Status.RELEASED, release_date, user
    order.save(update_fields=["status", "released_at", "updated_by", "updated_at"])
    logger.info("Released production order %s", order.order_number)
    return order


@transaction.atomic
def cancel_order(order, user=None) -> ProductionOrder:
    """Cancel an order that nothing has been posted to yet."""
    order = _lock(order)
    if order.status not in (ProductionOrder.Status.CREATED, ProductionOrder.Status.RELEASED):
        raise ProductionError(f"Order {order.order_number} is {order.get_status_display().lower()}.")
    if _order_lines(order).exists() or order.confirmations.exists():
        raise ProductionError(f"Order {order.order_number} has postings; complete it instead of cancelling.")
    order.status, order.updated_by = ProductionOrder.Status.CANCELLED, user
    order.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Cancelled production order %s", order.order_number)
    return order


# --- goods movements -------------------------------------------------------------------------

@dataclass
class IssueInput:
    """One component to issue. Without a storage location, the component's default is used."""

    material: object
    quantity: Decimal
    storage_location: object = None


def _movement_quantities(order, movement_type) -> dict[int, Decimal]:
    """Material id -> quantity moved for the order (reversals included), as a positive number."""
    rows = (MaterialDocumentItem.objects
            .filter(document__company_code=order.company_code, document__order_number=order.order_number,
                    document__movement_type=movement_type)
            .values("material_id").annotate(quantity=Sum("quantity")))
    sign = -1 if movement_type == MovementType.GOODS_ISSUE_ORDER else 1
    return {row["material_id"]: _quantity(sign * row["quantity"]) for row in rows}


def issued_quantities(order) -> dict[int, Decimal]:
    """Component material id -> quantity issued to the order so far."""
    return _movement_quantities(order, MovementType.GOODS_ISSUE_ORDER)


def delivered_quantity(order) -> Decimal:
    """Quantity of the product received from the order so far."""
    return _movement_quantities(order, MovementType.GOODS_RECEIPT).get(order.material_id, Decimal("0.000"))


@transaction.atomic
def issue_components(order, posting_date: date, items: list[IssueInput] | None = None, user=None):
    """Issue components to the order (movement 261). Without ``items``, issue what is still required.

    Issuing more than required is allowed (it shows up as a quantity variance); issuing materials
    that are not components of the order is not.
    """
    order = _lock(order)
    _require_released(order)
    components = {c.material_id: c for c in order.components.select_related("material", "storage_location")}
    if items is None:
        issued = issued_quantities(order)
        items = [IssueInput(c.material, c.required_quantity - issued.get(c.material_id, 0), c.storage_location)
                 for c in components.values() if c.required_quantity > issued.get(c.material_id, 0)]
        if not items:
            raise ProductionError(f"Every component of order {order.order_number} has already been issued.")
    stock_items = []
    for item in items:
        component = components.get(item.material.pk)
        if component is None:
            raise ProductionError(f"{item.material.number} is not a component of order {order.order_number}.")
        stock_items.append(inventory.ItemInput(item.material, item.storage_location or component.storage_location,
                                               _quantity(item.quantity)))
    document = inventory.post_goods_movement(
        movement_type=MovementType.GOODS_ISSUE_ORDER, plant=order.plant, posting_date=posting_date,
        items=stock_items, reference=order.order_number, header_text=f"Issue to order {order.order_number}",
        order_number=order.order_number, user=user,
    )
    logger.info("Issued %d component(s) to production order %s with %s", len(stock_items), order.order_number,
                document.document_number)
    return document


@transaction.atomic
def receive_output(order, posting_date: date, quantity: Decimal, storage_location=None, user=None):
    """Receive finished goods from the order (movement 101) at the order's planned unit cost."""
    order = _lock(order)
    _require_released(order)
    quantity = _quantity(quantity)
    if quantity <= 0:
        raise ProductionError("Quantity must be greater than zero.")
    open_quantity = order.quantity - delivered_quantity(order)
    if quantity > open_quantity:
        raise ProductionError(f"Order {order.order_number} has {open_quantity} still to deliver; "
                              f"{quantity} would be an over-delivery.")
    document = inventory.post_goods_movement(
        movement_type=MovementType.GOODS_RECEIPT, plant=order.plant, posting_date=posting_date,
        items=[inventory.ItemInput(order.material, storage_location or order.receipt_storage_location, quantity,
                                   unit_price=order.planned_unit_cost)],
        reference=order.order_number, header_text=f"Receipt from order {order.order_number}",
        order_number=order.order_number, user=user,
    )
    logger.info("Received %s x %s from production order %s with %s", quantity, order.material.number,
                order.order_number, document.document_number)
    return document


# --- confirmations ---------------------------------------------------------------------------

def _confirmed_activity(operation) -> dict[int, Decimal]:
    """Activity type id -> quantity already confirmed on an operation."""
    rows = (ConfirmationActivity.objects.filter(confirmation__operation=operation)
            .values("activity_type_id").annotate(quantity=Sum("quantity")))
    return {row["activity_type_id"]: row["quantity"] for row in rows}


@transaction.atomic
def confirm_operation(order, operation_number: int, posting_date: date, *, yield_quantity=Decimal("0"),
                      scrap_quantity=Decimal("0"), activities: dict | None = None, final: bool = False,
                      external_id: str = "", user=None) -> Confirmation:
    """Report work on an operation and charge its activities to the order.

    ``activities`` maps activity type codes to the quantity worked. Without it, the planned activity
    is used: in proportion to the quantity reported, or, for a final confirmation, whatever is still
    planned. ``external_id`` (e.g. the MES event ID) makes the call idempotent (ADR 0010): the same
    ID again returns the confirmation posted the first time, so a message delivered twice is charged
    once and the sender gets the same answer both times.
    """
    order = _lock(order)
    if external_id:
        # Idempotency (ADR 0010): the same event again returns the confirmation it created the first
        # time, even if the order has since been completed. Checked before any other rule for that reason.
        existing = order.confirmations.select_related("operation").filter(external_id=external_id).first()
        if existing is not None:
            if existing.operation.operation_number != operation_number:
                raise ProductionError(f"Event {external_id} was already used for operation "
                                      f"{existing.operation.operation_number} of order {order.order_number}.")
            logger.info("Confirmation event %s for order %s was already posted as %s; returning it",
                        external_id, order.order_number, existing.confirmation_number)
            return existing
    _require_released(order)
    operation = order.operations.select_related("work_center__cost_center").filter(
        operation_number=operation_number).first()
    if operation is None:
        raise ProductionError(f"Order {order.order_number} has no operation {operation_number}.")
    yield_quantity, scrap_quantity = _quantity(yield_quantity), _quantity(scrap_quantity)
    if yield_quantity < 0 or scrap_quantity < 0:
        raise ProductionError("Yield and scrap cannot be negative.")
    if operation.confirmations.filter(is_final=True).exists():
        raise ProductionError(f"Operation {operation_number} of order {order.order_number} is already finally "
                              "confirmed.")

    work_center = operation.work_center
    allowed = {a.code: a for a in work_center.activity_types.all()}
    planned = {a.activity_type_id: a for a in operation.activities.select_related("activity_type")}
    worked = []  # (activity type, quantity)
    if activities is None:
        done = _confirmed_activity(operation)
        for activity_id, plan in planned.items():
            if final:
                quantity = plan.planned_quantity - done.get(activity_id, 0)
            else:
                quantity = plan.planned_quantity * (yield_quantity + scrap_quantity) / order.quantity
            if _quantity(quantity) > 0:
                worked.append((plan.activity_type, _quantity(quantity)))
    else:
        for code, quantity in activities.items():
            if code not in allowed:
                raise ProductionError(f"Activity {code} is not recorded at work centre {work_center.code}.")
            if Decimal(quantity) < 0:
                raise ProductionError(f"Activity {code} cannot be negative.")
            if Decimal(quantity) > 0:
                worked.append((allowed[code], _quantity(quantity)))
    if not worked and yield_quantity == 0 and scrap_quantity == 0 and not final:
        raise ProductionError("Nothing to confirm: give a yield, scrap or activity quantity.")

    priced = []
    for activity_type, quantity in worked:
        rate = services.activity_rate(work_center.cost_center, activity_type, posting_date)
        priced.append((activity_type, quantity, rate, _money(quantity * rate)))

    number = f"{order.confirmations.count() + 1:04d}"
    journal_entry = None
    lines = []
    for activity_type, quantity, _rate, amount in priced:
        if amount == 0:
            continue
        text = f"{order.order_number}/{operation_number:04d} {activity_type.code} {quantity}"
        account = activity_type.allocation_account
        lines.append(LineInput(account, "D", amount, plant=order.plant, material=order.material, text=text,
                               order_number=order.order_number))
        lines.append(LineInput(account, "C", amount, cost_center=work_center.cost_center, text=text))
    if lines:
        journal_entry = finance.post_journal_entry(
            company_code=order.company_code, document_type=DocumentType.PRODUCTION_CONFIRMATION,
            posting_date=posting_date, reference=order.order_number,
            header_text=f"Confirmation {number} of order {order.order_number}", lines=lines, user=user,
        )
    confirmation = Confirmation.objects.create(
        order=order, operation=operation, confirmation_number=number, posting_date=posting_date,
        yield_quantity=yield_quantity, scrap_quantity=scrap_quantity, is_final=final,
        journal_entry=journal_entry, external_id=external_id, created_by=user, updated_by=user,
    )
    ConfirmationActivity.objects.bulk_create([
        ConfirmationActivity(confirmation=confirmation, activity_type=activity_type, quantity=quantity, rate=rate,
                             amount=amount)
        for activity_type, quantity, rate, amount in priced
    ])
    logger.info("Confirmation %s of order %s, operation %s at %s: yield %s, scrap %s, %s charged%s",
                number, order.order_number, operation_number, work_center.code, yield_quantity, scrap_quantity,
                sum((p[3] for p in priced), ZERO), " (final)" if final else "")
    return confirmation


# --- completion and settlement ---------------------------------------------------------------

def _order_lines(order):
    return JournalLine.objects.filter(company_code=order.company_code, order_number=order.order_number)


def order_balance(order) -> Decimal:
    """Actual cost on the order minus the value credited for goods received (positive = debit)."""
    total = _order_lines(order).aggregate(total=Sum("amount_local"))["total"]
    return _money(total or 0)


@transaction.atomic
def complete_order(order, posting_date: date, user=None) -> ProductionOrder:
    """Close the order (SAP TECO + settlement) and move its remaining balance to production variances."""
    order = _lock(order)
    _require_released(order)
    balance = order_balance(order)
    entry = None
    if balance != 0:
        material_type = order.material.material_type
        output = inventory.account_for(order.company_code, AccountKey.FACTORY_OUTPUT, material_type)
        variance = inventory.account_for(order.company_code, AccountKey.PROD_VARIANCE, material_type)
        text = f"Settlement of order {order.order_number}"
        order_side, variance_side = ("C", "D") if balance > 0 else ("D", "C")
        entry = finance.post_journal_entry(
            company_code=order.company_code, document_type=DocumentType.PRODUCTION_SETTLEMENT,
            posting_date=posting_date, reference=order.order_number, header_text=text, user=user,
            lines=[
                LineInput(output, order_side, abs(balance), plant=order.plant, material=order.material, text=text,
                          order_number=order.order_number),
                LineInput(variance, variance_side, abs(balance), plant=order.plant, material=order.material,
                          text=text),
            ],
        )
    order.status, order.completed_at = ProductionOrder.Status.COMPLETED, posting_date
    order.settlement_entry, order.updated_by = entry, user
    order.save(update_fields=["status", "completed_at", "settlement_entry", "updated_by", "updated_at"])
    logger.info("Completed production order %s: variance %s%s", order.order_number, balance,
                f" settled with {entry.document_number}" if entry else "")
    return order


# --- report ----------------------------------------------------------------------------------

def order_costs(order) -> dict:
    """Planned against actual cost of an order, by component, activity and output (SAP KKBC)."""
    order = ProductionOrder.objects.select_related("material", "company_code__currency").get(pk=order.pk)
    lines = _order_lines(order)
    consumption = {row["material_id"]: _money(row["value"]) for row in
                   lines.filter(entry__document_type=DocumentType.GOODS_ISSUE)
                   .values("material_id").annotate(value=Sum("amount_local"))}
    issued = issued_quantities(order)
    components = []
    for c in order.components.select_related("material__base_unit"):
        components.append({
            "material": c.material.number, "unit": c.material.base_unit.code,
            "planned_quantity": c.required_quantity, "actual_quantity": issued.get(c.material_id, Decimal("0.000")),
            "planned_value": c.planned_value, "actual_value": consumption.get(c.material_id, ZERO),
        })
    actual_activity = {}
    for row in (ConfirmationActivity.objects.filter(confirmation__order=order)
                .values("confirmation__operation__operation_number", "activity_type__code")
                .annotate(quantity=Sum("quantity"), value=Sum("amount"))):
        actual_activity[(row["confirmation__operation__operation_number"], row["activity_type__code"])] = row
    activities = []
    for op in order.operations.select_related("work_center").prefetch_related("activities__activity_type"):
        for plan in op.activities.all():
            actual = actual_activity.get((op.operation_number, plan.activity_type.code), {})
            activities.append({
                "operation_number": op.operation_number, "work_center": op.work_center.code,
                "activity_type": plan.activity_type.code,
                "planned_quantity": plan.planned_quantity, "actual_quantity": _quantity(actual.get("quantity") or 0),
                "planned_value": plan.planned_value, "actual_value": _money(actual.get("value") or 0),
            })
    delivered = delivered_quantity(order)
    output_credit = -_money(lines.filter(entry__document_type=DocumentType.GOODS_RECEIPT)
                            .aggregate(v=Sum("amount_local"))["v"] or 0)
    actual_cost = (sum((c["actual_value"] for c in components), ZERO)
                   + sum((a["actual_value"] for a in activities), ZERO))
    settled = -_money(lines.filter(entry__document_type=DocumentType.PRODUCTION_SETTLEMENT)
                      .aggregate(v=Sum("amount_local"))["v"] or 0)
    return {
        "order": order.order_number, "material": order.material.number, "status": order.status,
        "currency": order.company_code.currency.code,
        "quantity": order.quantity, "delivered_quantity": delivered,
        "planned_cost": order.planned_cost, "planned_unit_cost": order.planned_unit_cost,
        "actual_cost": actual_cost, "output_credit": output_credit,
        "balance": order_balance(order), "settled_variance": settled,
        # Target cost: what the quantity actually delivered should have cost at plan.
        "target_cost": _money(order.planned_unit_cost * delivered),
        "components": components, "activities": activities,
    }
