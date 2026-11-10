"""Production planning rules: equipment, bills of materials, routings and the standard cost estimate.

Every rule that protects the data lives here, so the API, the admin, commands and Wave 5b's
production orders all go through the same checks:

* equipment codes are unique per plant and a work centre's cost centre and activity types belong to
  the plant's company code;
* a BOM may not contain its own material, directly or through any number of levels (no cycles);
* a routing's operations run at active work centres of the same plant and only record activity types
  assigned to that work centre.

Rule violations raise ``ProductionError`` (a ``PostingError``), which the API returns as a 400 and the
exception handler logs with the file and line of the rule.
"""
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from apps.finance.services import PostingError
from apps.inventory import services as inventory
from apps.masterdata.models import Material

from .models import (
    ActivityRate,
    BillOfMaterials,
    BOMItem,
    OperationActivity,
    ProductionLine,
    Routing,
    RoutingOperation,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
QUANTITY = Decimal("0.001")
MAX_LEVELS = 10  # deepest BOM structure explored; deeper structures are almost certainly an error
PRODUCED_TYPES = (Material.MaterialType.FINISHED, Material.MaterialType.SEMI_FINISHED)


class ProductionError(PostingError):
    """A production planning rule was broken."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _quantity(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY, rounding=ROUND_HALF_UP)


# --- equipment hierarchy ---------------------------------------------------------------------

def check_line_code(area, code: str, exclude_pk=None):
    """Line codes are unique within the plant, not only within the area: they are graph keys (plant/code)."""
    clash = ProductionLine.objects.filter(area__plant=area.plant, code=code)
    if exclude_pk:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        raise ProductionError(f"Line {code} already exists in plant {area.plant.code}.")


def check_work_center(plant, line, cost_center, activity_types=()):
    """A work centre's line, cost centre and activity types must all belong to its plant's company code."""
    company_code = plant.company_code
    if line.area.plant_id != plant.id:
        raise ProductionError(f"Line {line.code} is not in plant {plant.code}.")
    if cost_center.company_code_id != company_code.id:
        raise ProductionError(f"Cost centre {cost_center.code} is not in company code {company_code.code}.")
    if not cost_center.is_active:
        raise ProductionError(f"Cost centre {cost_center.code} is inactive.")
    # Production cost centres only: a sales or admin cost centre would be relieved of production costs.
    if cost_center.category != cost_center.Category.PRODUCTION:
        raise ProductionError(f"Cost centre {cost_center.code} is not a production cost centre.")
    for activity_type in activity_types:
        if activity_type.company_code_id != company_code.id:
            raise ProductionError(f"Activity type {activity_type.code} is not in company code {company_code.code}.")


def equipment_hierarchy(plant) -> dict:
    """The plant's ISA-95 equipment tree with each level's UNS topic path (``GET /api/production/equipment/``)."""
    company_code = plant.company_code
    areas = []
    for area in plant.production_areas.order_by("code").prefetch_related(
        "lines__work_centers__activity_types", "lines__work_centers__cost_center"
    ):
        lines = []
        for line in area.lines.order_by("code"):
            cells = [{
                "code": wc.code, "name": wc.name, "isa95_level": wc.isa95_level,
                "cost_center": wc.cost_center.code,
                "activity_types": sorted(a.code for a in wc.activity_types.all()),
                "is_active": wc.is_active, "uns_path": wc.uns_path,
            } for wc in sorted(line.work_centers.all(), key=lambda w: w.code)]
            lines.append({"code": line.code, "name": line.name, "isa95_level": line.isa95_level,
                          "isa95_type": line.isa95_type, "is_active": line.is_active,
                          "uns_path": line.uns_path, "work_centers": cells})
        areas.append({"code": area.code, "name": area.name, "isa95_level": area.isa95_level,
                      "is_active": area.is_active, "uns_path": area.uns_path, "lines": lines})
    return {
        "enterprise": company_code.code, "site": plant.code, "name": plant.name,
        "uns_path": f"{company_code.code.lower()}/{plant.code.lower()}", "areas": areas,
    }


# --- activity rates --------------------------------------------------------------------------

def activity_rate(cost_center, activity_type, on: date) -> Decimal:
    """The rate valid on ``on``: the latest ``valid_from`` on or before that date."""
    rate = (ActivityRate.objects.filter(cost_center=cost_center, activity_type=activity_type, valid_from__lte=on)
            .order_by("-valid_from").first())
    if rate is None:
        raise ProductionError(
            f"No rate for activity {activity_type.code} at cost centre {cost_center.code} on {on}."
        )
    return rate.price


# --- bills of materials ----------------------------------------------------------------------

@dataclass
class ComponentInput:
    """One component of a BOM to create. Item numbers default to 10, 20, 30..."""

    component: Material
    quantity: Decimal
    item_number: int | None = None


def _check_produced(material, plant, kind):
    if material.material_type not in PRODUCED_TYPES:
        raise ProductionError(
            f"{material.number} is a {material.get_material_type_display().lower()}; only finished and "
            f"semi-finished products have a {kind}."
        )
    if not material.is_active:
        raise ProductionError(f"Material {material.number} is inactive.")


def _numbered(entries, attribute, kind):
    """Fill in default numbers (10, 20, ...) and reject duplicates."""
    numbers = []
    for index, entry in enumerate(entries, start=1):
        if getattr(entry, attribute) is None:
            setattr(entry, attribute, index * 10)
        numbers.append(getattr(entry, attribute))
    if len(numbers) != len(set(numbers)):
        raise ProductionError(f"Each {kind} number may appear only once.")


def _bom_children(plant) -> dict[int, set[int]]:
    """Material id -> component ids, over every active BOM version in the plant."""
    children: dict[int, set[int]] = {}
    for header, component in BOMItem.objects.filter(
        bom__plant=plant, bom__status=BillOfMaterials.Status.ACTIVE
    ).values_list("bom__material_id", "component_id"):
        children.setdefault(header, set()).add(component)
    return children


def _check_no_cycle(material, plant, component_ids):
    """Refuse a BOM whose components lead back to its own material at any depth.

    All active versions count, whatever their dates, because a cycle in any combination of
    versions would make an explosion or a cost roll-up loop forever.
    """
    children = _bom_children(plant)
    stack = [(cid, [cid]) for cid in component_ids]
    seen = set()
    while stack:
        current, path = stack.pop()
        if current == material.id:
            numbers = Material.objects.in_bulk(path)
            chain = " -> ".join([material.number] + [numbers[i].number for i in path])
            raise ProductionError(f"This BOM would make {material.number} a component of itself: {chain}.")
        if current in seen:
            continue
        seen.add(current)
        stack.extend((child, path + [child]) for child in children.get(current, ()))


@transaction.atomic
def create_bom(*, material, plant, valid_from: date, items: list[ComponentInput], base_quantity=Decimal("1"),
               description="", user=None) -> BillOfMaterials:
    """Create a BOM version. Components are checked for type, duplicates and cycles."""
    _check_produced(material, plant, "BOM")
    if not items:
        raise ProductionError("A BOM needs at least one component.")
    _numbered(items, "item_number", "item")
    components = [entry.component for entry in items]
    if len({c.pk for c in components}) != len(components):
        raise ProductionError("Each component may appear only once; add the quantities together.")
    for entry in items:
        if entry.component.pk == material.pk:
            raise ProductionError(f"{material.number} cannot be a component of itself.")
        if not entry.component.is_stock_managed:
            raise ProductionError(f"{entry.component.number} is a service and cannot be a BOM component.")
        if not entry.component.is_active:
            raise ProductionError(f"Component {entry.component.number} is inactive.")
        if entry.quantity <= 0:
            raise ProductionError(f"Quantity of {entry.component.number} must be greater than zero.")
    if BillOfMaterials.objects.filter(material=material, plant=plant, valid_from=valid_from).exists():
        raise ProductionError(f"A BOM for {material.number} in plant {plant.code} valid from {valid_from} exists; "
                              "create the new version with a later date.")
    _check_no_cycle(material, plant, [c.pk for c in components])

    bom = BillOfMaterials.objects.create(material=material, plant=plant, valid_from=valid_from,
                                         base_quantity=base_quantity, description=description,
                                         created_by=user, updated_by=user)
    BOMItem.objects.bulk_create([
        BOMItem(bom=bom, item_number=entry.item_number, component=entry.component, quantity=entry.quantity)
        for entry in items
    ])
    logger.info("Created BOM %s: %d component(s)", bom.key, len(items))
    return bom


@transaction.atomic
def set_bom_status(bom: BillOfMaterials, status: str, user=None) -> BillOfMaterials:
    """Activate or deactivate a BOM version. Reactivating checks for cycles again."""
    if status == BillOfMaterials.Status.ACTIVE and bom.status != status:
        _check_no_cycle(bom.material, bom.plant, list(bom.items.values_list("component_id", flat=True)))
    bom.status = status
    bom.updated_by = user
    bom.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("BOM %s set to %s", bom.key, status)
    return bom


def find_bom(material, plant, on: date) -> BillOfMaterials | None:
    """The active BOM valid on ``on`` (latest ``valid_from`` on or before it), or None."""
    return (BillOfMaterials.objects.filter(material=material, plant=plant, status=BillOfMaterials.Status.ACTIVE,
                                           valid_from__lte=on).order_by("-valid_from").first())


def explode_bom(material, plant, quantity: Decimal, on: date) -> list[dict]:
    """Every component needed for ``quantity`` of ``material``, level by level (SAP CS11).

    A component with its own BOM is exploded further; its row says so with ``has_bom``. Quantities
    are scaled from each BOM's base quantity and rounded to 0.001.
    """
    bom = find_bom(material, plant, on)
    if bom is None:
        raise ProductionError(f"No active BOM for {material.number} in plant {plant.code} on {on}.")
    rows = []

    def walk(current_bom, required, level, path):
        if level > MAX_LEVELS:
            raise ProductionError(f"BOM of {material.number} is deeper than {MAX_LEVELS} levels.")
        for item in current_bom.items.select_related("component__base_unit").order_by("item_number"):
            needed = _quantity(item.quantity * required / current_bom.base_quantity)
            sub_bom = find_bom(item.component, plant, on)
            rows.append({
                "level": level, "path": "/".join(path + [item.component.number]), "item_number": item.item_number,
                "component": item.component.number, "description": item.component.description,
                "material_type": item.component.material_type, "unit": item.component.base_unit.code,
                "quantity": needed, "has_bom": sub_bom is not None,
            })
            if sub_bom is not None:
                walk(sub_bom, needed, level + 1, path + [item.component.number])

    walk(bom, Decimal(quantity), 1, [material.number])
    return rows


# --- routings --------------------------------------------------------------------------------

@dataclass
class ActivityInput:
    """Planned activity of an operation: fixed setup plus a quantity per base quantity."""

    activity_type: object
    setup_quantity: Decimal = Decimal("0")
    quantity_per_base: Decimal = Decimal("0")


@dataclass
class OperationInput:
    """One operation of a routing to create. Numbers default to 10, 20, 30..."""

    description: str
    work_center: object
    activities: list[ActivityInput] = field(default_factory=list)
    operation_number: int | None = None


@transaction.atomic
def create_routing(*, material, plant, valid_from: date, operations: list[OperationInput],
                   base_quantity=Decimal("1"), description="", user=None) -> Routing:
    """Create a routing version: operations at active work centres of the plant, with planned activities."""
    _check_produced(material, plant, "routing")
    if not operations:
        raise ProductionError("A routing needs at least one operation.")
    _numbered(operations, "operation_number", "operation")
    for op in operations:
        work_center = op.work_center
        if work_center.plant_id != plant.id:
            raise ProductionError(f"Work centre {work_center.code} is not in plant {plant.code}.")
        if not work_center.is_active:
            raise ProductionError(f"Work centre {work_center.code} is inactive.")
        allowed = set(work_center.activity_types.values_list("pk", flat=True))
        seen = set()
        for activity in op.activities:
            code = activity.activity_type.code
            if activity.activity_type.pk not in allowed:
                raise ProductionError(f"Activity {code} is not recorded at work centre {work_center.code}.")
            if activity.activity_type.pk in seen:
                raise ProductionError(f"Activity {code} appears twice in operation {op.operation_number}.")
            seen.add(activity.activity_type.pk)
            if activity.setup_quantity < 0 or activity.quantity_per_base < 0:
                raise ProductionError(f"Activity quantities of {code} cannot be negative.")
            if activity.setup_quantity == 0 and activity.quantity_per_base == 0:
                raise ProductionError(f"Activity {code} in operation {op.operation_number} has no quantity.")
    if Routing.objects.filter(material=material, plant=plant, valid_from=valid_from).exists():
        raise ProductionError(f"A routing for {material.number} in plant {plant.code} valid from {valid_from} "
                              "exists; create the new version with a later date.")

    routing = Routing.objects.create(material=material, plant=plant, valid_from=valid_from,
                                     base_quantity=base_quantity, description=description,
                                     created_by=user, updated_by=user)
    for op in operations:
        operation = RoutingOperation.objects.create(routing=routing, operation_number=op.operation_number,
                                                    description=op.description, work_center=op.work_center)
        OperationActivity.objects.bulk_create([
            OperationActivity(operation=operation, activity_type=a.activity_type, setup_quantity=a.setup_quantity,
                              quantity_per_base=a.quantity_per_base)
            for a in op.activities
        ])
    logger.info("Created routing %s: %d operation(s)", routing.key, len(operations))
    return routing


def set_routing_status(routing: Routing, status: str, user=None) -> Routing:
    """Activate or deactivate a routing version."""
    routing.status = status
    routing.updated_by = user
    routing.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Routing %s set to %s", routing.key, status)
    return routing


def find_routing(material, plant, on: date) -> Routing | None:
    """The active routing valid on ``on`` (latest ``valid_from`` on or before it), or None."""
    return (Routing.objects.filter(material=material, plant=plant, status=Routing.Status.ACTIVE,
                                   valid_from__lte=on).order_by("-valid_from").first())


def planned_activity(activity: OperationActivity, quantity: Decimal, base_quantity: Decimal) -> Decimal:
    """Setup plus the variable part scaled to ``quantity`` (rounded to 0.001)."""
    return _quantity(activity.setup_quantity + activity.quantity_per_base * quantity / base_quantity)


# --- standard cost estimate ------------------------------------------------------------------

def cost_estimate(material, plant, quantity: Decimal, on: date) -> dict:
    """What ``quantity`` of ``material`` should cost to make on ``on`` (a simplified SAP CK11N).

    * Materials: each component at its moving average price in the plant. A component without
      stock but with its own BOM is costed by rolling up its own estimate, so semi-finished goods
      that are made and used straight away still get a cost.
    * Activities: each routing activity at the rate of the work centre's cost centre.

    Wave 5b compares production orders with this estimate to show variances.
    """
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ProductionError("Quantity must be greater than zero.")
    result = _estimate(material, plant, quantity, on, level=1)
    logger.info("Cost estimate for %s x %s in plant %s on %s: %s", quantity, material.number, plant.code, on,
                result["total"])
    return result


def _estimate(material, plant, quantity: Decimal, on: date, level: int) -> dict:
    if level > MAX_LEVELS:
        raise ProductionError(f"Cost roll-up of {material.number} is deeper than {MAX_LEVELS} levels.")
    bom = find_bom(material, plant, on)
    if bom is None:
        raise ProductionError(f"No active BOM for {material.number} in plant {plant.code} on {on}.")

    materials = []
    for item in bom.items.select_related("component__base_unit").order_by("item_number"):
        component = item.component
        needed = _quantity(item.quantity * quantity / bom.base_quantity)
        stock_quantity, stock_value = inventory.plant_stock(component, plant)
        price = inventory.moving_average_price(stock_quantity, stock_value)
        if price is not None:
            value, source, detail = _money(needed * price), "moving_average", None
        elif find_bom(component, plant, on) is not None:
            detail = _estimate(component, plant, needed, on, level + 1)
            value, source = detail["total"], "rolled_up"
            price = (value / needed).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        else:
            raise ProductionError(f"No price for {component.number} in plant {plant.code}: there is no stock to "
                                  "value it and no BOM to roll it up.")
        materials.append({"item_number": item.item_number, "component": component.number,
                          "description": component.description, "unit": component.base_unit.code,
                          "quantity": needed, "price": price, "value": value, "source": source,
                          "estimate": detail})

    activities = []
    routing = find_routing(material, plant, on)
    if routing is not None:
        for operation in routing.operations.select_related("work_center__cost_center").order_by("operation_number"):
            for activity in operation.activities.select_related("activity_type__unit"):
                planned = planned_activity(activity, quantity, routing.base_quantity)
                rate = activity_rate(operation.work_center.cost_center, activity.activity_type, on)
                activities.append({
                    "operation_number": operation.operation_number, "description": operation.description,
                    "work_center": operation.work_center.code,
                    "cost_center": operation.work_center.cost_center.code,
                    "activity_type": activity.activity_type.code, "unit": activity.activity_type.unit.code,
                    "quantity": planned, "rate": rate, "value": _money(planned * rate),
                })

    material_cost = sum((m["value"] for m in materials), Decimal("0.00"))
    activity_cost = sum((a["value"] for a in activities), Decimal("0.00"))
    total = material_cost + activity_cost
    return {
        "material": material.number, "plant": plant.code, "quantity": quantity, "date": on,
        "bom": bom.key, "routing": routing.key if routing else None,
        "materials": materials, "activities": activities,
        "material_cost": material_cost, "activity_cost": activity_cost, "total": total,
        "unit_cost": (total / quantity).quantize(CENT, rounding=ROUND_HALF_UP),
        "currency": plant.company_code.currency.code,
    }
