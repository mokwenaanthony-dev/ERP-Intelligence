"""Order-to-cash business rules.

    create_sales_order -> confirm_order -> post_delivery (601 goods issue) -> post_invoice -> payment (finance)

Each step validates its input, posts its accounting through the stock ledger or the journal
in the same database transaction, and leaves an immutable trail that can be reversed
(``reverse_delivery``, ``cancel_invoice``) but never edited.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from apps.finance import services as finance
from apps.finance.models import DocumentType, JournalLine, TaxCode
from apps.graph import outbox
from apps.inventory import services as inventory
from apps.inventory.models import MovementType
from apps.masterdata.models import CustomerRole, Material
from apps.organization.models import Plant, StorageLocation

from .models import (
    Delivery,
    DeliveryItem,
    Invoice,
    InvoiceItem,
    RevenueAccountSetting,
    SalesOrder,
    SalesOrderItem,
    SalesPrice,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# Sales documents are numbered continuously per company code (not restarted each fiscal year),
# so the number range uses "fiscal year" 0.
CONTINUOUS = 0


def _next_number(company_code, prefix: str) -> str:
    return finance.next_document_number(company_code, CONTINUOUS, prefix)


class SalesError(finance.PostingError):
    """The request breaks an order-to-cash rule and nothing was saved."""


@dataclass
class OrderItemInput:
    material: Material
    quantity: Decimal
    plant: Plant | None = None  # defaults to the first plant of the company code
    net_price: Decimal | None = None  # None = determine from the price list
    tax_code: TaxCode | None = None  # None = default for the material type


@dataclass
class DeliveryItemInput:
    order_item: SalesOrderItem
    quantity: Decimal
    storage_location: StorageLocation


# --- Pricing and account determination ---------------------------------------------------

def price_for(material, sales_organization, on: date) -> Decimal:
    price = SalesPrice.objects.filter(
        material=material, sales_organization=sales_organization, valid_from__lte=on
    ).order_by("-valid_from").first()
    if price is None:
        raise SalesError(f"No price for {material.number} in sales organisation {sales_organization.code} on {on}.")
    return price.price


def revenue_setting(company_code, material) -> RevenueAccountSetting:
    settings = {
        s.material_type: s for s in RevenueAccountSetting.objects.select_related(
            "revenue_account", "default_tax_code__gl_account"
        ).filter(chart_of_accounts=company_code.chart_of_accounts, material_type__in=[material.material_type, ""])
    }
    setting = settings.get(material.material_type) or settings.get("")
    if setting is None:
        raise SalesError(f"No revenue account is set up for material type {material.material_type}.")
    return setting


def _customer_role(company_code, customer) -> CustomerRole:
    role = CustomerRole.objects.select_related("reconciliation_account").filter(
        business_partner=customer, company_code=company_code
    ).first()
    if role is None:
        raise SalesError(f"Business partner {customer.number} is not a customer of company code {company_code.code}.")
    if customer.is_blocked:
        raise SalesError(f"Customer {customer.number} is blocked.")
    return role


# --- Sales orders --------------------------------------------------------------------------

def _build_items(order, items: list[OrderItemInput], user):
    if not items:
        raise SalesError("A sales order needs at least one item.")
    company_code = order.company_code
    default_plant = Plant.objects.filter(company_code=company_code).order_by("code").first()
    rows = []
    for number, item in enumerate(items, start=1):
        material = item.material
        if not material.is_active:
            raise SalesError(f"Material {material.number} is not active.")
        if item.quantity is None or item.quantity <= 0:
            raise SalesError("Quantities must be greater than zero.")
        plant = item.plant or default_plant
        if plant is None or plant.company_code_id != company_code.pk:
            raise SalesError(f"Plant {plant.code if plant else '-'} does not belong to {company_code.code}.")
        setting = revenue_setting(company_code, material)
        tax_code = item.tax_code or setting.default_tax_code
        if tax_code.chart_of_accounts_id != company_code.chart_of_accounts_id or tax_code.tax_type != "OUTPUT":
            raise SalesError(f"Tax code {tax_code.code} is not an output tax code of {company_code.code}.")
        overridden = item.net_price is not None
        if overridden and item.net_price < 0:
            raise SalesError("Prices cannot be negative.")
        price = item.net_price if overridden else price_for(material, order.sales_organization, order.order_date)
        rows.append(SalesOrderItem(
            order=order, item_number=number * 10, material=material, plant=plant, quantity=item.quantity,
            unit=material.base_unit, net_price=Decimal(price).quantize(CENT), price_overridden=overridden,
            tax_code=tax_code, created_by=user, updated_by=user,
        ))
    SalesOrderItem.objects.bulk_create(rows)


@transaction.atomic
def create_sales_order(*, sales_organization, customer, order_date: date, items: list[OrderItemInput],
                       requested_delivery_date: date | None = None, customer_reference: str = "",
                       user=None) -> SalesOrder:
    company_code = sales_organization.company_code
    _customer_role(company_code, customer)
    order = SalesOrder.objects.create(
        company_code=company_code, sales_organization=sales_organization, customer=customer,
        order_number=_next_number(company_code, "SO"), order_date=order_date,
        requested_delivery_date=requested_delivery_date, customer_reference=customer_reference,
        created_by=user, updated_by=user,
    )
    _build_items(order, items, user)
    return order


@transaction.atomic
def replace_order_items(order: SalesOrder, items: list[OrderItemInput], user=None) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != SalesOrder.Status.OPEN:
        raise SalesError(f"Items can only change while the order is OPEN (it is {order.status}).")
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
def confirm_order(order: SalesOrder, user=None) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != SalesOrder.Status.OPEN:
        raise SalesError(f"Only OPEN orders can be confirmed (this one is {order.status}).")
    _customer_role(order.company_code, order.customer)
    return _set_status(order, SalesOrder.Status.CONFIRMED, user)


@transaction.atomic
def cancel_order(order: SalesOrder, user=None) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status in (SalesOrder.Status.COMPLETED, SalesOrder.Status.CANCELLED):
        raise SalesError(f"A {order.status} order cannot be cancelled.")
    if order.deliveries.filter(status=Delivery.Status.POSTED).exists():
        raise SalesError("The order has deliveries; reverse them before cancelling.")
    return _set_status(order, SalesOrder.Status.CANCELLED, user)


def _refresh_completion(order, user):
    """COMPLETED once every item is fully delivered and invoiced; back to CONFIRMED otherwise."""
    items = list(order.items.all())
    done = all(i.delivered_quantity == i.quantity and i.invoiced_quantity == i.quantity for i in items)
    if done and order.status == SalesOrder.Status.CONFIRMED:
        _set_status(order, SalesOrder.Status.COMPLETED, user)
    elif not done and order.status == SalesOrder.Status.COMPLETED:
        _set_status(order, SalesOrder.Status.CONFIRMED, user)


# --- Deliveries --------------------------------------------------------------------------

@transaction.atomic
def post_delivery(order: SalesOrder, *, delivery_date: date, items: list[DeliveryItemInput] | None = None,
                  storage_location: StorageLocation | None = None, user=None) -> Delivery:
    """Deliver order items and post the goods issue (601), which books cost of sales.

    Without ``items`` every open quantity is delivered from ``storage_location``.
    """
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != SalesOrder.Status.CONFIRMED:
        raise SalesError(f"Only CONFIRMED orders can be delivered (this one is {order.status}).")
    if items is None:
        if storage_location is None:
            raise SalesError("Give the items to deliver or a storage location to deliver everything from.")
        items = [DeliveryItemInput(i, i.open_quantity, storage_location) for i in order.items.all() if i.open_quantity]
    if not items:
        raise SalesError("Nothing left to deliver on this order.")

    plants = {item.order_item.plant_id for item in items}
    if len(plants) != 1:
        raise SalesError("One delivery ships from one plant; split the items into separate deliveries.")
    plant = items[0].order_item.plant
    for item in items:
        if item.order_item.order_id != order.pk:
            raise SalesError(f"Item {item.order_item} does not belong to order {order.order_number}.")
        if item.quantity <= 0:
            raise SalesError("Delivery quantities must be greater than zero.")
        if item.quantity > item.order_item.open_quantity:
            raise SalesError(
                f"Item {item.order_item.item_number}: delivering {item.quantity} exceeds the open quantity "
                f"{item.order_item.open_quantity}."
            )

    goods_issue = inventory.post_goods_movement(
        movement_type=MovementType.GOODS_ISSUE_DELIVERY, plant=plant, posting_date=delivery_date,
        reference=order.order_number, header_text=f"Delivery for {order.order_number}",
        business_partner=order.customer, user=user,
        items=[inventory.ItemInput(item.order_item.material, item.storage_location, item.quantity,
                                   text=f"{order.order_number}/{item.order_item.item_number}") for item in items],
    )
    delivery = Delivery.objects.create(
        company_code=order.company_code, delivery_number=_next_number(order.company_code, "DL"),
        sales_order=order, plant=plant, delivery_date=delivery_date, material_document=goods_issue,
        created_by=user, updated_by=user,
    )
    DeliveryItem.objects.bulk_create([
        DeliveryItem(delivery=delivery, order_item=item.order_item, storage_location=item.storage_location,
                     quantity=item.quantity)
        for item in items
    ])
    return delivery


@transaction.atomic
def reverse_delivery(delivery: Delivery, *, posting_date: date | None = None, user=None) -> Delivery:
    """Reverse the goods issue (stock comes back, cost of sales is reversed). Not allowed once invoiced."""
    delivery = Delivery.objects.select_for_update().get(pk=delivery.pk)
    if delivery.status != Delivery.Status.POSTED:
        raise SalesError(f"Delivery {delivery.delivery_number} is already reversed.")
    if delivery.invoices.filter(status=Invoice.Status.POSTED).exists():
        raise SalesError(f"Delivery {delivery.delivery_number} has been invoiced; cancel the invoice first.")
    inventory.reverse_material_document(delivery.material_document, posting_date=posting_date, user=user)
    Delivery.objects.filter(pk=delivery.pk).update(status=Delivery.Status.REVERSED, updated_by=user)
    outbox.emit(delivery)
    delivery.refresh_from_db()
    _refresh_completion(delivery.sales_order, user)
    return delivery


# --- Invoices ----------------------------------------------------------------------------

@transaction.atomic
def post_invoice(delivery: Delivery, *, invoice_date: date, user=None) -> Invoice:
    """Bill a delivery: Dr receivables (gross) / Cr revenue (net) / Cr output VAT, per item."""
    delivery = Delivery.objects.select_for_update().select_related(
        "sales_order__company_code__chart_of_accounts", "sales_order__customer"
    ).get(pk=delivery.pk)
    if delivery.status != Delivery.Status.POSTED:
        raise SalesError(f"Delivery {delivery.delivery_number} is reversed and cannot be invoiced.")
    if delivery.invoices.filter(status=Invoice.Status.POSTED).exists():
        raise SalesError(f"Delivery {delivery.delivery_number} has already been invoiced.")
    order = delivery.sales_order
    company_code, customer = order.company_code, order.customer
    role = _customer_role(company_code, customer)

    rows, lines = [], []
    for item in delivery.items.select_related("order_item__material", "order_item__tax_code__gl_account"):
        order_item = item.order_item
        net = (item.quantity * order_item.net_price).quantize(CENT)
        tax = finance.calculate_tax(net, order_item.tax_code)
        rows.append((order_item, item.quantity, net, tax))
        revenue = revenue_setting(company_code, order_item.material).revenue_account
        lines.append(finance.LineInput(
            revenue, JournalLine.Side.CREDIT, net, business_partner=customer, material=order_item.material,
            plant=delivery.plant, tax_code=order_item.tax_code,
            text=f"{order.order_number}/{order_item.item_number} {order_item.material.number} x {item.quantity}",
        ))
    net_total = sum((r[2] for r in rows), ZERO)
    tax_total = sum((r[3] for r in rows), ZERO)
    gross = net_total + tax_total
    lines.insert(0, finance.LineInput(role.reconciliation_account, JournalLine.Side.DEBIT, gross,
                                      business_partner=customer, text=f"Invoice for {order.order_number}"))

    invoice_number = _next_number(company_code, "IN")
    entry = finance.post_journal_entry(
        company_code=company_code, document_type=DocumentType.CUSTOMER_INVOICE, posting_date=invoice_date,
        reference=invoice_number, header_text=f"Invoice {invoice_number} for {order.order_number}"[:100],
        lines=lines, user=user,
    )
    invoice = Invoice.objects.create(
        invoice_number=invoice_number, company_code=company_code, customer=customer, delivery=delivery,
        invoice_date=invoice_date, due_date=invoice_date + timedelta(days=role.payment_terms_days),
        net_amount=net_total, tax_amount=tax_total, gross_amount=gross, journal_entry=entry,
        created_by=user, updated_by=user,
    )
    InvoiceItem.objects.bulk_create([
        InvoiceItem(invoice=invoice, order_item=order_item, quantity=quantity, net_price=order_item.net_price,
                    net_amount=net, tax_code=order_item.tax_code, tax_amount=tax)
        for order_item, quantity, net, tax in rows
    ])
    _refresh_completion(order, user)
    return invoice


@transaction.atomic
def cancel_invoice(invoice: Invoice, *, posting_date: date | None = None, user=None) -> Invoice:
    """Reverse the invoice's journal entry. Only while nothing has been paid against it."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != Invoice.Status.POSTED:
        raise SalesError(f"Invoice {invoice.invoice_number} is already cancelled.")
    receivable = invoice.journal_entry.lines.get(line_number=1)
    if receivable.clearings.filter(clearing_entry__status="POSTED", clearing_entry__reversal_of__isnull=True).exists():
        raise SalesError(f"Invoice {invoice.invoice_number} has payments; reverse the payment first.")
    finance.reverse_journal_entry(invoice.journal_entry, posting_date=posting_date, user=user)
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.CANCELLED, updated_by=user)
    outbox.emit(invoice)
    invoice.refresh_from_db()
    _refresh_completion(invoice.delivery.sales_order, user)
    return invoice


def invoice_open_amount(invoice: Invoice) -> Decimal:
    """Gross amount still unpaid, from the open-item view of the receivable line."""
    if invoice.status != Invoice.Status.POSTED:
        return ZERO
    receivable = invoice.journal_entry.lines.get(line_number=1)
    items = finance.open_items(invoice.company_code, partner=invoice.customer, include_cleared=True)
    for item in items:
        if item["line_id"] == receivable.pk:
            return item["open_amount"]
    return ZERO  # pragma: no cover - the receivable line is always an open item while the invoice is posted
