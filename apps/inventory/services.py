"""Inventory business rules: goods movements, moving-average valuation and their accounting.

Stock quantity and value per material and plant are always computed from the
material document items (sums of signed amounts), so they can never drift
from the movements that created them. Each value-changing movement posts a
journal entry through ``apps.finance.services`` in the same database
transaction, so stock value and the inventory G/L account always agree.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
import logging

from django.db import transaction
from django.db.models import Sum

from apps.finance import services as finance
from apps.finance.models import DocumentType, GLAccount, JournalLine
from apps.finance.services import LineInput, PostingError
from apps.masterdata.models import Material, MaterialPlant
from apps.organization.models import Plant, StorageLocation

from .models import (
    OFFSET_ACCOUNT,
    ORDER_RECEIPT_OFFSET,
    PRICED_RECEIPTS,
    RECEIPTS,
    AccountKey,
    InventoryAccountSetting,
    MaterialDocument,
    MaterialDocumentItem,
    MovementType,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO_QTY = Decimal("0.000")
ZERO = Decimal("0.00")

JOURNAL_DOCUMENT_TYPE = {
    MovementType.GOODS_RECEIPT: DocumentType.GOODS_RECEIPT,
    MovementType.INITIAL_ENTRY: DocumentType.GOODS_RECEIPT,
    MovementType.GOODS_ISSUE_DELIVERY: DocumentType.GOODS_ISSUE,
    MovementType.SCRAPPING: DocumentType.GOODS_ISSUE,
    MovementType.INVENTORY_GAIN: DocumentType.INVENTORY_ADJUSTMENT,
    MovementType.INVENTORY_LOSS: DocumentType.INVENTORY_ADJUSTMENT,
    MovementType.GOODS_ISSUE_ORDER: DocumentType.GOODS_ISSUE,
}


class StockError(PostingError):
    """The movement breaks an inventory rule and was not posted."""


@dataclass
class ItemInput:
    """One item of a goods movement to post. Quantity is positive; the movement type gives the direction."""

    material: Material
    storage_location: StorageLocation
    quantity: Decimal  # positive, in the material's base unit
    unit_price: Decimal | None = None  # required for 101 and 561; optional for 701 when there is no stock
    to_storage_location: StorageLocation | None = None  # 311 only
    text: str = ""


# --- Stock and valuation (always derived from the items) -----------------------------------

QTY = Decimal("0.001")


def _qty(value) -> Decimal:
    """Database totals rounded to quantity precision. SQLite sums decimals as floats, so a total of
    zero can come back as -7E-13; PostgreSQL is exact. Rounding makes both behave the same."""
    return Decimal(value or 0).quantize(QTY)


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def plant_stock(material, plant) -> tuple[Decimal, Decimal]:
    """(quantity, value) of a material in a plant."""
    totals = MaterialDocumentItem.objects.filter(material=material, plant=plant).aggregate(
        quantity=Sum("quantity"), value=Sum("amount_local")
    )
    return _qty(totals["quantity"]), _money(totals["value"])


def location_stock(material, storage_location) -> Decimal:
    """Quantity of a material in one storage location."""
    total = MaterialDocumentItem.objects.filter(
        material=material, storage_location=storage_location
    ).aggregate(quantity=Sum("quantity"))["quantity"]
    return _qty(total)


def moving_average_price(quantity: Decimal, value: Decimal) -> Decimal | None:
    """Value divided by quantity, or None when there is no stock to price."""
    if quantity <= 0:
        return None
    return (value / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def issue_value(quantity: Decimal, stock_quantity: Decimal, stock_value: Decimal) -> Decimal:
    """Value of an issue at moving average price. Issuing everything takes the full remaining value."""
    if quantity == stock_quantity:
        return stock_value
    return (stock_value * quantity / stock_quantity).quantize(CENT, rounding=ROUND_HALF_UP)


# --- Account determination -------------------------------------------------------------

def account_for(company_code, key: str, material_type: str) -> GLAccount:
    """G/L account for an account key (e.g. BSX inventory, GBB offset) and material type; a
    setting for the specific material type wins over the default (blank) one.
    """
    settings = InventoryAccountSetting.objects.select_related("gl_account").filter(
        chart_of_accounts=company_code.chart_of_accounts, account_key=key, material_type__in=[material_type, ""]
    )
    specific = {s.material_type: s.gl_account for s in settings}
    account = specific.get(material_type) or specific.get("")
    if account is None:
        raise StockError(
            f"No {key} account is set up for material type {material_type} in chart "
            f"{company_code.chart_of_accounts.code}."
        )
    return account


# --- Posting -------------------------------------------------------------------------

def _validate(movement_type, plant, items, order_number=""):
    if movement_type not in MovementType.values:
        raise StockError(f"Unknown movement type {movement_type}.")
    # Production orders (Wave 5b): 261 issues to an order, 101 can also receive from one.
    if movement_type == MovementType.GOODS_ISSUE_ORDER and not order_number:
        raise StockError("A goods issue to a production order (261) needs the order number.")
    if order_number and movement_type not in (MovementType.GOODS_ISSUE_ORDER, MovementType.GOODS_RECEIPT):
        raise StockError(f"Movement {movement_type} cannot refer to a production order.")
    if not items:
        raise StockError("A goods movement needs at least one item.")
    for item in items:
        material = item.material
        if not material.is_active:
            raise StockError(f"Material {material.number} is not active.")
        if not material.is_stock_managed:
            raise StockError(f"Material {material.number} is a service and is not kept in stock.")
        if item.quantity is None or item.quantity <= 0:
            raise StockError("Quantities must be greater than zero.")
        if item.storage_location.plant_id != plant.pk:
            raise StockError(f"Storage location {item.storage_location} is not in plant {plant.code}.")
        if movement_type in PRICED_RECEIPTS and (item.unit_price is None or item.unit_price < 0):
            raise StockError(f"Movement {movement_type} needs a unit price for material {material.number}.")
        if movement_type == MovementType.TRANSFER:
            target = item.to_storage_location
            if target is None:
                raise StockError("A transfer needs a destination storage location.")
            if target.plant_id != plant.pk:
                raise StockError(f"Storage location {target} is not in plant {plant.code}.")
            if target.pk == item.storage_location.pk:
                raise StockError("Source and destination storage locations must differ.")


def _lock_material_plants(items, plant):
    """Serialise movements per material and plant (row lock on the plant view)."""
    for material_id in sorted({item.material.pk for item in items}):
        MaterialPlant.objects.get_or_create(material_id=material_id, plant=plant)
    list(MaterialPlant.objects.select_for_update().filter(material_id__in={i.material.pk for i in items}, plant=plant))


def _stock_effects(movement_type, plant, items):
    """Turn input items into signed (item, storage_location, quantity, value) effects, checking stock."""
    effects = []
    # Running totals so several items for the same material in one document are valued correctly.
    plant_totals = {}
    location_totals = {}

    def totals(material):
        """Running plant totals for a material, loaded once per document."""
        if material.pk not in plant_totals:
            plant_totals[material.pk] = list(plant_stock(material, plant))
        return plant_totals[material.pk]

    def location_quantity(material, location):
        """Running storage-location quantity for a material, loaded once per document."""
        key = (material.pk, location.pk)
        if key not in location_totals:
            location_totals[key] = location_stock(material, location)
        return location_totals[key]

    for item in items:
        material, quantity = item.material, item.quantity
        stock = totals(material)
        if movement_type == MovementType.TRANSFER:
            available = location_quantity(material, item.storage_location)
            if quantity > available:
                raise StockError(
                    f"Insufficient stock of {material.number} in {item.storage_location}: "
                    f"need {quantity}, have {available}."
                )
            location_totals[(material.pk, item.storage_location.pk)] = available - quantity
            target_key = (material.pk, item.to_storage_location.pk)
            location_totals[target_key] = location_quantity(material, item.to_storage_location) + quantity
            effects.append((item, item.storage_location, -quantity, ZERO))
            effects.append((item, item.to_storage_location, quantity, ZERO))
            continue

        if movement_type in RECEIPTS:
            if movement_type in PRICED_RECEIPTS or item.unit_price is not None:
                value = (quantity * item.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
            elif stock[0] > 0:
                value = (stock[1] * quantity / stock[0]).quantize(CENT, rounding=ROUND_HALF_UP)
            else:
                raise StockError(
                    f"Material {material.number} has no stock in plant {plant.code} to value the gain; "
                    "give a unit price."
                )
            signed_quantity, signed_value = quantity, value
        else:
            available = location_quantity(material, item.storage_location)
            if quantity > available:
                raise StockError(
                    f"Insufficient stock of {material.number} in {item.storage_location}: "
                    f"need {quantity}, have {available}."
                )
            value = issue_value(quantity, stock[0], stock[1])
            signed_quantity, signed_value = -quantity, -value

        stock[0] += signed_quantity
        stock[1] += signed_value
        location_totals[(material.pk, item.storage_location.pk)] = (
            location_quantity(material, item.storage_location) + signed_quantity
        )
        effects.append((item, item.storage_location, signed_quantity, signed_value))
    return effects


def _journal_lines(company_code, movement_type, plant, effects, business_partner, order_number=""):
    """Inventory line and offset line per material, values in company currency.

    With a production order, the offset line is charged to the order (its cost object): consumption
    for a 261, factory output for a 101 receipt from the order.
    """
    offset_key = OFFSET_ACCOUNT[movement_type]
    if order_number and movement_type == MovementType.GOODS_RECEIPT:
        offset_key = ORDER_RECEIPT_OFFSET
    lines = []
    for item, _location, _quantity, value in effects:
        if value == 0:
            continue
        material = item.material
        inventory = account_for(company_code, AccountKey.INVENTORY, material.material_type)
        offset = account_for(company_code, offset_key, material.material_type)
        side, opposite = (JournalLine.Side.DEBIT, JournalLine.Side.CREDIT) if value > 0 else (
            JournalLine.Side.CREDIT, JournalLine.Side.DEBIT
        )
        text = f"{movement_type} {material.number} x {abs(item.quantity)}"
        lines.append(LineInput(inventory, side, abs(value), material=material, plant=plant, text=text))
        lines.append(LineInput(offset, opposite, abs(value), material=material, plant=plant,
                               business_partner=business_partner, text=text, order_number=order_number))
    return lines


@transaction.atomic
def post_goods_movement(*, movement_type: str, plant: Plant, posting_date: date, items: list[ItemInput],
                        document_date: date | None = None, reference: str = "", header_text: str = "",
                        business_partner=None, order_number: str = "", user=None) -> MaterialDocument:
    """Post a goods movement and, if it changes stock value, its journal entry. All or nothing."""
    company_code = plant.company_code
    _validate(movement_type, plant, items, order_number)
    period = finance.find_period(company_code, posting_date)
    if not period.is_open:
        raise StockError(f"Posting period {period} is closed.")

    _lock_material_plants(items, plant)
    effects = _stock_effects(movement_type, plant, items)

    journal_entry = None
    lines = _journal_lines(company_code, movement_type, plant, effects, business_partner, order_number) \
        if movement_type != MovementType.TRANSFER else []
    if lines:
        journal_entry = finance.post_journal_entry(
            company_code=company_code, document_type=JOURNAL_DOCUMENT_TYPE[movement_type],
            posting_date=posting_date, document_date=document_date, reference=reference,
            header_text=header_text or f"Goods movement {movement_type}", lines=lines, user=user,
        )

    fiscal_year = period.fiscal_year.year
    document = MaterialDocument.objects.create(
        company_code=company_code, plant=plant, fiscal_year=fiscal_year,
        document_number=finance.next_document_number(company_code, fiscal_year, "MD"),
        movement_type=movement_type, posting_date=posting_date, document_date=document_date or posting_date,
        reference=reference, header_text=header_text, business_partner=business_partner,
        order_number=order_number, journal_entry=journal_entry, created_by=user, updated_by=user,
    )
    MaterialDocumentItem.objects.bulk_create([
        MaterialDocumentItem(
            document=document, line_number=number, material=item.material, plant=plant,
            storage_location=location, quantity=quantity, unit=item.material.base_unit,
            amount_local=value, text=item.text,
        )
        for number, (item, location, quantity, value) in enumerate(effects, start=1)
    ])
    logger.info("Posted material document %s (movement %s) at plant %s: %d item(s)",
                document.document_number, movement_type, plant.code, len(effects))
    return document


@transaction.atomic
def reverse_material_document(document: MaterialDocument, *, posting_date: date | None = None,
                              user=None) -> MaterialDocument:
    """Post the exact opposite stock effects (same quantities and values) and reverse the journal entry."""
    document = MaterialDocument.objects.select_for_update().get(pk=document.pk)
    if document.status == MaterialDocument.Status.REVERSED:
        raise StockError(f"Material document {document} has already been reversed.")
    if document.reversal_of_id is not None:
        raise StockError(f"Material document {document} is itself a reversal and cannot be reversed.")

    posting_date = posting_date or document.posting_date
    company_code = document.company_code
    period = finance.find_period(company_code, posting_date)
    if not period.is_open:
        raise StockError(f"Posting period {period} is closed.")

    items = list(document.items.select_related("material", "storage_location", "unit"))
    for material_id in sorted({item.material_id for item in items}):
        MaterialPlant.objects.select_for_update().get(material_id=material_id, plant=document.plant)

    # Undoing a receipt must not take stock below zero anywhere.
    location_change = defaultdict(Decimal)
    for item in items:
        location_change[(item.material, item.storage_location)] -= item.quantity
    for (material, location), change in location_change.items():
        if change < 0 and location_stock(material, location) + change < 0:
            raise StockError(
                f"Cannot reverse {document.document_number}: {material.number} in {location} "
                "has already been issued."
            )
    # Nor leave a value behind with no quantity (or a negative value) in the plant.
    plant_change = defaultdict(lambda: [ZERO_QTY, ZERO])
    for item in items:
        plant_change[item.material][0] -= item.quantity
        plant_change[item.material][1] -= item.amount_local
    for material, (quantity_change, value_change) in plant_change.items():
        quantity, value = plant_stock(material, document.plant)
        quantity, value = quantity + quantity_change, value + value_change
        if (quantity == 0 and value != 0) or value < 0:
            raise StockError(
                f"Cannot reverse {document.document_number}: it would leave {material.number} in plant "
                f"{document.plant.code} with quantity {quantity} and value {value}. Post an inventory adjustment."
            )

    journal_entry = None
    if document.journal_entry_id:
        journal_entry = finance.reverse_journal_entry(document.journal_entry, posting_date=posting_date, user=user)

    fiscal_year = period.fiscal_year.year
    reversal = MaterialDocument.objects.create(
        company_code=company_code, plant=document.plant, fiscal_year=fiscal_year,
        document_number=finance.next_document_number(company_code, fiscal_year, "MD"),
        movement_type=document.movement_type, posting_date=posting_date, document_date=posting_date,
        reference=document.reference, header_text=f"Reversal of {document.document_number}"[:100],
        business_partner=document.business_partner, order_number=document.order_number,
        journal_entry=journal_entry, reversal_of=document,
        created_by=user, updated_by=user,
    )
    MaterialDocumentItem.objects.bulk_create([
        MaterialDocumentItem(
            document=reversal, line_number=item.line_number, material=item.material, plant=item.plant,
            storage_location=item.storage_location, quantity=-item.quantity, unit=item.unit,
            amount_local=-item.amount_local, text=item.text,
        )
        for item in items
    ])
    MaterialDocument.objects.filter(pk=document.pk).update(status=MaterialDocument.Status.REVERSED, updated_by=user)
    logger.info("Reversed material document %s with %s", document.document_number, reversal.document_number)
    return reversal


# --- Reporting -----------------------------------------------------------------------

def stock_overview(*, plant=None, material=None) -> list[dict]:
    """Quantity per storage location, plus value and moving average price per material and plant."""
    items = MaterialDocumentItem.objects.all()
    if plant is not None:
        items = items.filter(plant=plant)
    if material is not None:
        items = items.filter(material=material)

    per_plant = items.values("material_id", "plant_id").annotate(quantity=Sum("quantity"), value=Sum("amount_local"))
    per_location = items.values("material_id", "plant_id", "storage_location__code").annotate(quantity=Sum("quantity"))
    locations = defaultdict(dict)
    for row in per_location:
        if _qty(row["quantity"]):
            locations[(row["material_id"], row["plant_id"])][row["storage_location__code"]] = _qty(row["quantity"])

    materials = Material.objects.select_related("base_unit").in_bulk({row["material_id"] for row in per_plant})
    plants = Plant.objects.in_bulk({row["plant_id"] for row in per_plant})
    result = []
    for row in per_plant:
        material_obj, plant_obj = materials[row["material_id"]], plants[row["plant_id"]]
        quantity, value = _qty(row["quantity"]), _money(row["value"])
        result.append({
            "material": material_obj.number, "description": material_obj.description,
            "plant": plant_obj.code, "unit": material_obj.base_unit.code,
            "quantity": quantity, "value": value,
            "moving_average_price": moving_average_price(quantity, value),
            "storage_locations": locations[(row["material_id"], row["plant_id"])],
        })
    return sorted(result, key=lambda r: (r["material"], r["plant"]))


def stock_reconciliation(company_code) -> dict:
    """Compare stock value (material documents) with the inventory G/L account balances (journal).

    In a correctly working system the difference is always zero.
    """
    stock_value = MaterialDocumentItem.objects.filter(document__company_code=company_code).aggregate(
        value=Sum("amount_local")
    )["value"] or ZERO
    inventory_accounts = InventoryAccountSetting.objects.filter(
        chart_of_accounts=company_code.chart_of_accounts, account_key=AccountKey.INVENTORY
    ).values_list("gl_account", flat=True)
    ledger_value = JournalLine.objects.filter(
        company_code=company_code, gl_account__in=inventory_accounts
    ).aggregate(value=Sum("amount_local"))["value"] or ZERO
    stock_value, ledger_value = _money(stock_value), _money(ledger_value)
    return {
        "company_code": company_code.code, "currency": company_code.currency.code,
        "stock_value": stock_value, "ledger_value": ledger_value,
        "difference": stock_value - ledger_value, "reconciled": stock_value == ledger_value,
    }
