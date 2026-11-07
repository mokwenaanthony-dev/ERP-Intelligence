"""Procure-to-pay business rules.

    create_purchase_order -> release_order -> post_goods_receipt (101) -> post_supplier_invoice -> payment

The goods receipt values stock at the order price and credits GR/IR. The supplier invoice is
checked against the order and the receipts (three-way match), clears GR/IR at the order price,
posts any price difference within tolerance to the price difference account, adds input VAT and
credits the supplier. Documents can be reversed (``reverse_goods_receipt``,
``cancel_supplier_invoice``) but never edited.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from apps.finance import services as finance
from apps.finance.models import DocumentType, JournalEntry, JournalLine, TaxCode
from apps.graph import outbox
from apps.inventory import services as inventory
from apps.inventory.models import AccountKey, InventoryAccountSetting, MovementType
from apps.masterdata.models import Material, SupplierRole
from apps.organization.models import Plant, StorageLocation

from .models import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchasingAccountSetting,
    SupplierInvoice,
    SupplierInvoiceItem,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
CONTINUOUS = 0  # purchasing documents are numbered continuously, like sales documents


def _next_number(company_code, prefix: str) -> str:
    return finance.next_document_number(company_code, CONTINUOUS, prefix)


class PurchasingError(finance.PostingError):
    """The request breaks a procure-to-pay rule and nothing was saved."""


@dataclass
class OrderItemInput:
    material: Material
    quantity: Decimal
    net_price: Decimal
    plant: Plant | None = None  # defaults to the first plant of the company code
    storage_location: StorageLocation | None = None  # defaults to the plant's first storage location
    tax_code: TaxCode | None = None  # defaults to the purchasing setting's input VAT code


@dataclass
class ReceiptItemInput:
    order_item: PurchaseOrderItem
    quantity: Decimal
    storage_location: StorageLocation | None = None  # defaults to the order item's storage location


@dataclass
class InvoiceItemInput:
    order_item: PurchaseOrderItem
    quantity: Decimal
    net_price: Decimal | None = None  # None = the order price


# --- Configuration and partner checks -----------------------------------------------------

def purchasing_setting(company_code) -> PurchasingAccountSetting:
    setting = PurchasingAccountSetting.objects.select_related(
        "default_tax_code__gl_account", "price_difference_account"
    ).filter(chart_of_accounts=company_code.chart_of_accounts).first()
    if setting is None:
        raise PurchasingError(f"Purchasing is not set up for chart {company_code.chart_of_accounts.code}.")
    return setting


def _supplier_role(company_code, supplier) -> SupplierRole:
    role = SupplierRole.objects.select_related("reconciliation_account").filter(
        business_partner=supplier, company_code=company_code
    ).first()
    if role is None:
        raise PurchasingError(
            f"Business partner {supplier.number} is not a supplier of company code {company_code.code}."
        )
    if supplier.is_blocked:
        raise PurchasingError(f"Supplier {supplier.number} is blocked.")
    return role


# --- Purchase orders -----------------------------------------------------------------------

def _build_items(order, items: list[OrderItemInput], user):
    if not items:
        raise PurchasingError("A purchase order needs at least one item.")
    company_code = order.company_code
    setting = purchasing_setting(company_code)
    default_plant = Plant.objects.filter(company_code=company_code).order_by("code").first()
    rows = []
    for number, item in enumerate(items, start=1):
        material = item.material
        if not material.is_active:
            raise PurchasingError(f"Material {material.number} is not active.")
        if not material.is_stock_managed:
            raise PurchasingError(f"Material {material.number} is a service; only stock materials can be ordered.")
        if item.quantity is None or item.quantity <= 0:
            raise PurchasingError("Quantities must be greater than zero.")
        if item.net_price is None or item.net_price < 0:
            raise PurchasingError(f"Give a price of zero or more for material {material.number}.")
        plant = item.plant or default_plant
        if plant is None or plant.company_code_id != company_code.pk:
            raise PurchasingError(f"Plant {plant.code if plant else '-'} does not belong to {company_code.code}.")
        location = item.storage_location or plant.storage_locations.order_by("code").first()
        if location is None or location.plant_id != plant.pk:
            raise PurchasingError(f"Storage location {location or '-'} is not in plant {plant.code}.")
        tax_code = item.tax_code or setting.default_tax_code
        if tax_code.chart_of_accounts_id != company_code.chart_of_accounts_id or tax_code.tax_type != "INPUT":
            raise PurchasingError(f"Tax code {tax_code.code} is not an input tax code of {company_code.code}.")
        rows.append(PurchaseOrderItem(
            order=order, item_number=number * 10, material=material, plant=plant, storage_location=location,
            quantity=item.quantity, unit=material.base_unit, net_price=Decimal(item.net_price).quantize(CENT),
            tax_code=tax_code, created_by=user, updated_by=user,
        ))
    PurchaseOrderItem.objects.bulk_create(rows)


@transaction.atomic
def create_purchase_order(*, purchasing_organization, supplier, order_date: date, items: list[OrderItemInput],
                          delivery_date: date | None = None, supplier_reference: str = "",
                          user=None) -> PurchaseOrder:
    company_code = purchasing_organization.company_code
    _supplier_role(company_code, supplier)
    order = PurchaseOrder.objects.create(
        company_code=company_code, purchasing_organization=purchasing_organization, supplier=supplier,
        order_number=_next_number(company_code, "PO"), order_date=order_date, delivery_date=delivery_date,
        supplier_reference=supplier_reference, created_by=user, updated_by=user,
    )
    _build_items(order, items, user)
    return order


@transaction.atomic
def replace_order_items(order: PurchaseOrder, items: list[OrderItemInput], user=None) -> PurchaseOrder:
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != PurchaseOrder.Status.OPEN:
        raise PurchasingError(f"Items can only change while the order is OPEN (it is {order.status}).")
    order.items.all().delete()
    _build_items(order, items, user)
    order.updated_by = user
    order.save(update_fields=["updated_by", "updated_at"])
    return order


def _set_status(order, status, user):
    order.status = status
    order.updated_by = user
    order.save(update_fields=["status", "updated_by", "updated_at"])
    return order


@transaction.atomic
def release_order(order: PurchaseOrder, user=None) -> PurchaseOrder:
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != PurchaseOrder.Status.OPEN:
        raise PurchasingError(f"Only OPEN orders can be released (this one is {order.status}).")
    _supplier_role(order.company_code, order.supplier)
    return _set_status(order, PurchaseOrder.Status.RELEASED, user)


@transaction.atomic
def cancel_order(order: PurchaseOrder, user=None) -> PurchaseOrder:
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    if order.status in (PurchaseOrder.Status.COMPLETED, PurchaseOrder.Status.CANCELLED):
        raise PurchasingError(f"A {order.status} order cannot be cancelled.")
    if order.receipts.filter(status=GoodsReceipt.Status.POSTED).exists():
        raise PurchasingError("The order has goods receipts; reverse them before cancelling.")
    return _set_status(order, PurchaseOrder.Status.CANCELLED, user)


def _refresh_completion(order, user):
    """COMPLETED once every item is fully received and invoiced; back to RELEASED otherwise."""
    items = list(order.items.all())
    done = all(i.received_quantity == i.quantity and i.invoiced_quantity == i.quantity for i in items)
    if done and order.status == PurchaseOrder.Status.RELEASED:
        _set_status(order, PurchaseOrder.Status.COMPLETED, user)
    elif not done and order.status == PurchaseOrder.Status.COMPLETED:
        _set_status(order, PurchaseOrder.Status.RELEASED, user)


# --- Goods receipts ------------------------------------------------------------------------

@transaction.atomic
def post_goods_receipt(order: PurchaseOrder, *, receipt_date: date, items: list[ReceiptItemInput] | None = None,
                       delivery_note: str = "", user=None) -> GoodsReceipt:
    """Receive goods against the order: a 101 movement at the order price (Dr inventory / Cr GR/IR).

    Without ``items`` every open quantity is received into each item's storage location.
    """
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != PurchaseOrder.Status.RELEASED:
        raise PurchasingError(f"Goods can only be received on RELEASED orders (this one is {order.status}).")
    if items is None:
        items = [ReceiptItemInput(i, i.open_quantity) for i in order.items.all() if i.open_quantity > 0]
    if not items:
        raise PurchasingError("Nothing left to receive on this order.")

    if len({item.order_item.plant_id for item in items}) != 1:
        raise PurchasingError("One goods receipt is for one plant; split the items into separate receipts.")
    plant = items[0].order_item.plant
    for item in items:
        if item.order_item.order_id != order.pk:
            raise PurchasingError(f"Item {item.order_item} does not belong to order {order.order_number}.")
        if item.quantity is None or item.quantity <= 0:
            raise PurchasingError("Receipt quantities must be greater than zero.")
        if item.quantity > item.order_item.open_quantity:
            raise PurchasingError(
                f"Item {item.order_item.item_number}: receiving {item.quantity} exceeds the open quantity "
                f"{item.order_item.open_quantity}."
            )
        item.storage_location = item.storage_location or item.order_item.storage_location

    document = inventory.post_goods_movement(
        movement_type=MovementType.GOODS_RECEIPT, plant=plant, posting_date=receipt_date,
        reference=order.order_number, header_text=f"Goods receipt for {order.order_number}",
        business_partner=order.supplier, user=user,
        items=[inventory.ItemInput(item.order_item.material, item.storage_location, item.quantity,
                                   unit_price=item.order_item.net_price,
                                   text=f"{order.order_number}/{item.order_item.item_number}") for item in items],
    )
    receipt = GoodsReceipt.objects.create(
        company_code=order.company_code, receipt_number=_next_number(order.company_code, "GR"),
        purchase_order=order, plant=plant, receipt_date=receipt_date, delivery_note=delivery_note,
        material_document=document, created_by=user, updated_by=user,
    )
    values = list(document.items.order_by("line_number").values_list("amount_local", flat=True))
    GoodsReceiptItem.objects.bulk_create([
        GoodsReceiptItem(receipt=receipt, order_item=item.order_item, storage_location=item.storage_location,
                         quantity=item.quantity, value=value)
        for item, value in zip(items, values)
    ])
    return receipt


@transaction.atomic
def reverse_goods_receipt(receipt: GoodsReceipt, *, posting_date: date | None = None, user=None) -> GoodsReceipt:
    """Reverse the 101 movement (stock and GR/IR go back). Not allowed if the goods are already invoiced."""
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != GoodsReceipt.Status.POSTED:
        raise PurchasingError(f"Goods receipt {receipt.receipt_number} is already reversed.")
    for item in receipt.items.select_related("order_item"):
        order_item = item.order_item
        if order_item.received_quantity - item.quantity < order_item.invoiced_quantity:
            raise PurchasingError(
                f"Goods receipt {receipt.receipt_number}: item {order_item.item_number} has been invoiced; "
                "cancel the supplier invoice first."
            )
    inventory.reverse_material_document(receipt.material_document, posting_date=posting_date, user=user)
    GoodsReceipt.objects.filter(pk=receipt.pk).update(status=GoodsReceipt.Status.REVERSED, updated_by=user)
    outbox.emit(receipt)
    receipt.refresh_from_db()
    _refresh_completion(receipt.purchase_order, user)
    return receipt


# --- Supplier invoices (logistics invoice verification) -----------------------------------

def _amount(quantity: Decimal, price: Decimal) -> Decimal:
    """Same rounding as the goods receipt valuation, so receipts and invoices agree."""
    return (quantity * price).quantize(CENT, rounding=ROUND_HALF_UP)


def _within_tolerance(invoice_price: Decimal, order_price: Decimal, tolerance: Decimal) -> bool:
    if order_price == 0:
        return invoice_price == 0
    return abs(invoice_price - order_price) * HUNDRED / order_price <= tolerance


def _line(account, amount: Decimal, *, supplier, order_item, tax_code, text):
    """A debit for a positive amount, a credit for a negative one."""
    side = JournalLine.Side.DEBIT if amount > 0 else JournalLine.Side.CREDIT
    return finance.LineInput(account, side, abs(amount), business_partner=supplier, material=order_item.material,
                             plant=order_item.plant, tax_code=tax_code, text=text)


@transaction.atomic
def post_supplier_invoice(order: PurchaseOrder, *, invoice_date: date, supplier_reference: str,
                          items: list[InvoiceItemInput] | None = None, user=None) -> SupplierInvoice:
    """Match the supplier's invoice to the order and receipts and post the payable.

    Per item: Dr GR/IR (quantity x order price), Dr/Cr price difference (quantity x the difference
    between invoice and order price) and input VAT on both; one credit to the supplier for the gross.
    Without ``items`` everything received but not yet invoiced is billed at the order price.
    """
    order = PurchaseOrder.objects.select_for_update().select_related(
        "company_code__chart_of_accounts", "supplier"
    ).get(pk=order.pk)
    if order.status not in (PurchaseOrder.Status.RELEASED, PurchaseOrder.Status.COMPLETED):
        raise PurchasingError(f"Order {order.order_number} is {order.status} and cannot be invoiced.")
    company_code, supplier = order.company_code, order.supplier
    role = _supplier_role(company_code, supplier)
    setting = purchasing_setting(company_code)
    supplier_reference = (supplier_reference or "").strip()
    if not supplier_reference:
        raise PurchasingError("Give the supplier's invoice number.")
    if SupplierInvoice.objects.filter(company_code=company_code, supplier=supplier,
                                      supplier_reference__iexact=supplier_reference,
                                      status=SupplierInvoice.Status.POSTED).exists():
        raise PurchasingError(f"Invoice {supplier_reference} from supplier {supplier.number} is already posted.")

    if items is None:
        items = [InvoiceItemInput(i, i.to_invoice_quantity) for i in order.items.all() if i.to_invoice_quantity > 0]
    if not items:
        raise PurchasingError(f"Nothing has been received on {order.order_number} that is not yet invoiced.")
    if len({item.order_item.pk for item in items}) != len(items):
        raise PurchasingError("Each order item can appear only once on an invoice.")

    gr_ir_accounts = {}
    rows, lines = [], []
    for item in items:
        order_item = item.order_item
        if order_item.order_id != order.pk:
            raise PurchasingError(f"Item {order_item} does not belong to order {order.order_number}.")
        if item.quantity is None or item.quantity <= 0:
            raise PurchasingError("Invoice quantities must be greater than zero.")
        # 1. Quantity: never bill more than was received.
        if item.quantity > order_item.to_invoice_quantity:
            raise PurchasingError(
                f"Item {order_item.item_number}: invoicing {item.quantity} but only "
                f"{order_item.to_invoice_quantity} has been received and not yet invoiced."
            )
        # 2. Price: the invoice price must be within tolerance of the order price.
        price = order_item.net_price if item.net_price is None else Decimal(item.net_price).quantize(CENT)
        if price < 0 or not _within_tolerance(price, order_item.net_price, setting.price_tolerance_percent):
            raise PurchasingError(
                f"Item {order_item.item_number}: invoice price {price} differs from the order price "
                f"{order_item.net_price} by more than {setting.price_tolerance_percent}%."
            )
        net = _amount(item.quantity, price)
        if item.quantity == order_item.to_invoice_quantity:
            # Last invoice for what was received: clear exactly what is left on GR/IR (no stray cents).
            gr_ir = order_item.received_value - order_item.invoiced_value
        else:
            gr_ir = _amount(item.quantity, order_item.net_price)
        difference = net - gr_ir
        tax_code = order_item.tax_code
        tax = finance.calculate_tax(gr_ir, tax_code)
        tax += (1 if difference > 0 else -1) * finance.calculate_tax(abs(difference), tax_code)
        rows.append((order_item, item.quantity, price, net, gr_ir, difference, tax))

        material_type = order_item.material.material_type
        if material_type not in gr_ir_accounts:
            gr_ir_accounts[material_type] = inventory.account_for(company_code, AccountKey.GR_IR, material_type)
        text = f"{order.order_number}/{order_item.item_number} {order_item.material.number} x {item.quantity}"
        if gr_ir:
            lines.append(_line(gr_ir_accounts[material_type], gr_ir, supplier=supplier, order_item=order_item,
                               tax_code=tax_code, text=text))
        if difference:
            lines.append(_line(setting.price_difference_account, difference, supplier=supplier,
                               order_item=order_item, tax_code=tax_code, text=f"Price difference {text}"[:100]))

    net_total = sum((r[3] for r in rows), ZERO)
    tax_total = sum((r[6] for r in rows), ZERO)
    gross = net_total + tax_total
    if gross <= 0:
        raise PurchasingError("The invoice total must be greater than zero.")
    lines.insert(0, finance.LineInput(role.reconciliation_account, JournalLine.Side.CREDIT, gross,
                                      business_partner=supplier, text=f"Invoice {supplier_reference}"[:100]))

    invoice_number = _next_number(company_code, "IV")
    entry = finance.post_journal_entry(
        company_code=company_code, document_type=DocumentType.SUPPLIER_INVOICE, posting_date=invoice_date,
        reference=supplier_reference[:30], lines=lines, user=user,
        header_text=f"Supplier invoice {invoice_number} for {order.order_number}"[:100],
    )
    invoice = SupplierInvoice.objects.create(
        invoice_number=invoice_number, company_code=company_code, supplier=supplier, purchase_order=order,
        supplier_reference=supplier_reference, invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=role.payment_terms_days),
        net_amount=net_total, tax_amount=tax_total, gross_amount=gross, journal_entry=entry,
        created_by=user, updated_by=user,
    )
    SupplierInvoiceItem.objects.bulk_create([
        SupplierInvoiceItem(invoice=invoice, order_item=order_item, quantity=quantity, net_price=price,
                            net_amount=net, gr_ir_amount=gr_ir, price_difference=difference,
                            tax_code=order_item.tax_code, tax_amount=tax)
        for order_item, quantity, price, net, gr_ir, difference, tax in rows
    ])
    _refresh_completion(order, user)
    return invoice


def _payable_line(invoice: SupplierInvoice) -> JournalLine:
    return invoice.journal_entry.lines.get(line_number=1)


@transaction.atomic
def cancel_supplier_invoice(invoice: SupplierInvoice, *, posting_date: date | None = None,
                            user=None) -> SupplierInvoice:
    """Reverse the invoice's journal entry (GR/IR is open again). Only while nothing has been paid."""
    invoice = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != SupplierInvoice.Status.POSTED:
        raise PurchasingError(f"Supplier invoice {invoice.invoice_number} is already cancelled.")
    if _payable_line(invoice).clearings.filter(clearing_entry__status=JournalEntry.Status.POSTED,
                                               clearing_entry__reversal_of__isnull=True).exists():
        raise PurchasingError(f"Supplier invoice {invoice.invoice_number} has payments; reverse the payment first.")
    finance.reverse_journal_entry(invoice.journal_entry, posting_date=posting_date, user=user)
    SupplierInvoice.objects.filter(pk=invoice.pk).update(status=SupplierInvoice.Status.CANCELLED, updated_by=user)
    outbox.emit(invoice)
    invoice.refresh_from_db()
    _refresh_completion(invoice.purchase_order, user)
    return invoice


def supplier_invoice_open_amount(invoice: SupplierInvoice) -> Decimal:
    """Gross amount still to pay, from the open-item view of the payable line."""
    if invoice.status != SupplierInvoice.Status.POSTED:
        return ZERO
    line_id = _payable_line(invoice).pk
    for item in finance.open_items(invoice.company_code, reconciliation_type="SUPPLIER", partner=invoice.supplier,
                                   include_cleared=True):
        if item["line_id"] == line_id:
            return item["open_amount"]
    return ZERO  # pragma: no cover - the payable line is always an open item while the invoice is posted


# --- GR/IR clearing --------------------------------------------------------------------------

def gr_ir_report(company_code) -> dict:
    """What is still on GR/IR, per order item (goods received not invoiced), versus the ledger.

    A positive balance means goods were received but the supplier has not billed them yet: the
    month-end accrual SAP shows in transaction MB5S.
    """
    items = (
        PurchaseOrderItem.objects.select_related("order__supplier", "material")
        .filter(order__company_code=company_code).order_by("order__order_number", "item_number")
    )
    rows, total = [], ZERO
    for item in items:
        received, invoiced = item.received_value, item.invoiced_value
        balance = received - invoiced
        if balance == 0 and item.received_quantity == item.invoiced_quantity:
            continue
        rows.append({
            "purchase_order": item.order.order_number, "item_number": item.item_number,
            "supplier": item.order.supplier.number, "supplier_name": item.order.supplier.name,
            "material": item.material.number, "received_quantity": item.received_quantity,
            "invoiced_quantity": item.invoiced_quantity, "received_value": received,
            "invoiced_value": invoiced, "balance": balance,
        })
        total += balance
    accounts = InventoryAccountSetting.objects.filter(
        chart_of_accounts=company_code.chart_of_accounts, account_key=AccountKey.GR_IR
    ).values_list("gl_account", flat=True)
    ledger = -finance._money(JournalLine.objects.filter(
        company_code=company_code, gl_account__in=list(accounts)
    ).aggregate(total=Sum("amount_local"))["total"])
    return {
        "company_code": company_code.code, "currency": company_code.currency.code, "items": rows,
        "open_balance": total, "ledger_balance": ledger, "difference": total - ledger, "reconciled": total == ledger,
    }
