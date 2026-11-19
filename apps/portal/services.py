"""The partner portal's rules (Wave 10a, ADR 0019): suppliers and customers plugged into the ERP.

Everything here starts from the user's ``PartnerAccess``: the business partner and company code it
acts for. Every query is filtered by that partner, so asking for another partner's document gives
"not found", exactly as if it did not exist (no hint that it does).

**Suppliers**

* see their purchase orders once released (an order still being entered is not theirs yet);
* **confirm** each line: quantity and delivery date. A confirmation is a fact the supplier tells us,
  kept as a history; the latest counts. Sending the same confirmation again adds nothing
  (idempotent); a changed date or quantity is a new confirmation. MRP plans the receipt on the
  confirmed date, and the control tower flags a confirmed date later than the one agreed.
* send **delivery notices** (what is on its way, and when it arrives), identified by their own
  delivery note number: the same notice again returns the first; a different notice under the same
  number is refused; the quantity on the way cannot exceed what is still open;
* **quote** on requests for quotation they were invited to, through the SRM rules (deadline, every
  item priced), never seeing anyone else's quote;
* read the **demand outlook** for the materials they supply, only while purchasing has agreed to
  share it with them.

**Customers** see their sales orders: items, what was delivered and invoiced, deliveries, invoices,
and whether an order line is late against the date they asked for.

Nothing a partner sends is executed on trust: a confirmation or notice informs planning; goods are
still received, checked and posted by the warehouse in the ERP. Logs carry partner and document
numbers only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.finance import services as finance
from apps.finance.services import PostingError
from apps.masterdata.models import SupplierRole
from apps.planning.models import ForecastPeriod, MaterialPlanning, PlannedOrder
from apps.planning.services import add_months, month_start
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem
from apps.sales.models import SalesOrder
from apps.srm import services as srm
from apps.srm.models import RequestForQuotation, SupplierQuote

from .models import DeliveryNotice, DeliveryNoticeItem, ForecastShare, OrderConfirmation, PartnerAccess

logger = logging.getLogger(__name__)

ZERO = Decimal("0.000")  # quantities keep their scale in every answer (ADR 0008)
THOUSANDTH = Decimal("0.001")
VISIBLE_PO = [PurchaseOrder.Status.RELEASED, PurchaseOrder.Status.COMPLETED, PurchaseOrder.Status.CANCELLED]


class PortalError(PostingError):
    """A portal rule was broken; nothing was recorded (the API answers 400)."""


class PortalConflict(PortalError):
    """The same reference was already used for something different (the API answers 409)."""


class NotFound(PortalError):
    """No such document for this partner (the API answers 404: another partner's looks the same)."""


class NotShared(PortalError):
    """Purchasing has not agreed to share the outlook with this supplier (the API answers 403)."""


def access_for(user) -> PartnerAccess:
    """The partner the user acts for; a partner user without a link may do nothing."""
    access = PartnerAccess.objects.select_related("partner", "company_code").filter(user=user).first()
    if access is None:
        raise NotFound("Your login is not linked to a business partner; ask the company to link it.")
    return access


# --- supplier: purchase orders and confirmations ------------------------------------------------------------

def _orders(access: PartnerAccess):
    return (PurchaseOrder.objects.filter(company_code=access.company_code, supplier=access.partner,
                                         status__in=VISIBLE_PO)
            .prefetch_related("items__material", "items__confirmations", "delivery_notices__items")
            .order_by("-order_date", "-id"))


def _order(access: PartnerAccess, number: str) -> PurchaseOrder:
    order = _orders(access).filter(order_number=number).first()
    if order is None:
        raise NotFound(f"Purchase order {number} was not found.")
    return order


def latest_confirmation(item) -> OrderConfirmation | None:
    """The confirmation that counts for a purchase order line (the newest), or None."""
    confirmations = list(item.confirmations.all())  # ordered newest first (Meta.ordering)
    return confirmations[0] if confirmations else None


def purchase_order_view(order: PurchaseOrder) -> dict:
    """A purchase order as its supplier sees it: lines, received, open, confirmed, notices."""
    lines = []
    for item in sorted(order.items.all(), key=lambda i: i.item_number):
        confirmed = latest_confirmation(item)
        lines.append({"item_number": item.item_number, "material": item.material.number,
                      "description": item.material.description, "quantity": item.quantity,
                      "net_price": item.net_price, "received": item.received_quantity,
                      "open": item.quantity - item.received_quantity,
                      "confirmed": ({"quantity": confirmed.quantity, "delivery_date": confirmed.delivery_date,
                                     "confirmed_at": confirmed.confirmed_at} if confirmed else None)})
    notices = [{"notice_number": n.notice_number, "reference": n.reference, "ship_date": n.ship_date,
                "arrival_date": n.arrival_date} for n in order.delivery_notices.all()]
    return {"order_number": order.order_number, "order_date": order.order_date,
            "delivery_date": order.delivery_date, "status": order.status, "items": lines, "notices": notices}


def purchase_orders(access: PartnerAccess) -> list[dict]:
    """The supplier's released, completed and cancelled purchase orders, newest first."""
    return [purchase_order_view(order) for order in _orders(access)]


def purchase_order(access: PartnerAccess, number: str) -> dict:
    """One of the supplier's purchase orders (404 for any other)."""
    return purchase_order_view(_order(access, number))


@dataclass(frozen=True)
class LineConfirmation:
    """What the supplier confirms for one line."""

    item_number: int
    quantity: Decimal
    delivery_date: date


def confirm(access: PartnerAccess, number: str, lines: list[LineConfirmation], *, user, note: str = "",
            now=None) -> tuple[dict, int]:
    """Record the supplier's confirmation of some or all lines; returns (the order, lines changed).

    A line whose latest confirmation already says the same is left alone, so the same request
    again changes nothing.
    """
    if not lines:
        raise PortalError("Confirm at least one line.")
    if len({line.item_number for line in lines}) != len(lines):
        raise PortalError("Confirm each line once per request.")
    with transaction.atomic():
        order = _order(access, number)
        if order.status != PurchaseOrder.Status.RELEASED:
            raise PortalError(f"Purchase order {number} is {order.status.lower()}: it cannot be confirmed.")
        items = {i.item_number: i for i in PurchaseOrderItem.objects.select_for_update()
                 .filter(order=order).prefetch_related("confirmations")}
        changed = 0
        for line in lines:
            item = items.get(line.item_number)
            if item is None:
                raise PortalError(f"Purchase order {number} has no line {line.item_number}.")
            open_quantity = item.quantity - item.received_quantity
            if line.quantity <= 0 or line.quantity > open_quantity:
                raise PortalError(f"Line {line.item_number}: confirm between 0 and the open {open_quantity}.")
            if line.delivery_date < order.order_date:
                raise PortalError(f"Line {line.item_number}: the date cannot be before the order date "
                                  f"{order.order_date}.")
            current = latest_confirmation(item)
            if current and (current.quantity, current.delivery_date) == (line.quantity, line.delivery_date):
                continue  # the same confirmation again: nothing to record
            OrderConfirmation.objects.create(order_item=item, quantity=line.quantity.quantize(THOUSANDTH),
                                             delivery_date=line.delivery_date, note=note[:200], confirmed_by=user,
                                             confirmed_at=now or timezone.now())
            changed += 1
    logger.info("Supplier %s confirmed %s: %d of %d line(s) changed", access.partner.number, number, changed,
                len(lines))
    return purchase_order(access, number), changed


# --- supplier: delivery notices -------------------------------------------------------------------------------

def notice_view(notice: DeliveryNotice) -> dict:
    """A delivery notice as the supplier and the warehouse see it."""
    return {"notice_number": notice.notice_number, "reference": notice.reference,
            "supplier": notice.supplier.number, "order_number": notice.purchase_order.order_number,
            "ship_date": notice.ship_date, "arrival_date": notice.arrival_date,
            "items": [{"item_number": i.order_item.item_number, "material": i.order_item.material.number,
                       "quantity": i.quantity} for i in notice.items.select_related("order_item__material")
                      .order_by("order_item__item_number")]}


def _same_notice(notice: DeliveryNotice, order: PurchaseOrder, ship: date, arrival: date, wanted: dict) -> bool:
    have = {i.order_item.item_number: i.quantity for i in notice.items.select_related("order_item")}
    return (notice.purchase_order_id, notice.ship_date, notice.arrival_date, have) == (
        order.pk, ship, arrival, wanted)


def send_notice(access: PartnerAccess, *, reference: str, order_number: str, ship_date: date, arrival_date: date,
                items: dict[int, Decimal], user) -> tuple[DeliveryNotice, bool]:
    """Record an advance shipping notice; returns (notice, created). Idempotent per supplier reference."""
    reference = (reference or "").strip()
    if not reference:
        raise PortalError("Give your delivery note number as the reference.")
    order = _order(access, order_number)
    wanted = {number: Decimal(q).quantize(THOUSANDTH) for number, q in items.items()}
    existing = DeliveryNotice.objects.filter(company_code=access.company_code, supplier=access.partner,
                                             reference=reference).first()
    if existing:
        if _same_notice(existing, order, ship_date, arrival_date, wanted):
            return existing, False
        raise PortalConflict(f"Delivery note {reference} was already announced as {existing.notice_number} with "
                             "other details; use a new reference for a different delivery.")
    if order.status != PurchaseOrder.Status.RELEASED:
        raise PortalError(f"Purchase order {order_number} is {order.status.lower()}: nothing more can be delivered.")
    if not wanted:
        raise PortalError("Say what is on its way: at least one line.")
    if arrival_date < ship_date:
        raise PortalError("The arrival date cannot be before the ship date.")
    lines = {i.item_number: i for i in order.items.all()}
    for number, quantity in wanted.items():
        item = lines.get(number)
        if item is None:
            raise PortalError(f"Purchase order {order_number} has no line {number}.")
        open_quantity = item.quantity - item.received_quantity
        if quantity <= 0 or quantity > open_quantity:
            raise PortalError(f"Line {number}: announce between 0 and the open {open_quantity}.")
    try:
        with transaction.atomic():
            notice = DeliveryNotice.objects.create(
                company_code=access.company_code, notice_number=finance.next_document_number(access.company_code, 0,
                                                                                             "AS"),
                supplier=access.partner, reference=reference, purchase_order=order, ship_date=ship_date,
                arrival_date=arrival_date, created_by=user, updated_by=user)
            DeliveryNoticeItem.objects.bulk_create([DeliveryNoticeItem(notice=notice, order_item=lines[n], quantity=q)
                                                    for n, q in wanted.items()])
    except IntegrityError:  # the same reference at the same moment: the first one stands
        existing = DeliveryNotice.objects.get(company_code=access.company_code, supplier=access.partner,
                                              reference=reference)
        if _same_notice(existing, order, ship_date, arrival_date, wanted):
            return existing, False
        raise PortalConflict(f"Delivery note {reference} was already announced as {existing.notice_number}.")
    logger.info("Delivery notice %s from supplier %s for %s, arriving %s", notice.notice_number,
                access.partner.number, order_number, arrival_date)
    return notice, True


def notices(access: PartnerAccess) -> list[dict]:
    """The supplier's own delivery notices, latest arrival first."""
    return [notice_view(n) for n in DeliveryNotice.objects.filter(company_code=access.company_code,
                                                                  supplier=access.partner)
            .select_related("supplier", "purchase_order")]


# --- supplier: requests for quotation -------------------------------------------------------------------------

def _quote(access: PartnerAccess, rfq_number: str) -> SupplierQuote:
    quote = (SupplierQuote.objects.filter(rfq__company_code=access.company_code, rfq__rfq_number=rfq_number,
                                          supplier=access.partner)
             .select_related("rfq").first())
    if quote is None:
        raise NotFound(f"Request for quotation {rfq_number} was not found.")
    return quote


def quote_view(quote: SupplierQuote) -> dict:
    """An RFQ as an invited supplier sees it: the items and its own quote, never anyone else's."""
    rfq = quote.rfq
    prices = {qi.rfq_item_id: qi.net_price for qi in quote.items.all()}
    return {"rfq_number": rfq.rfq_number, "title": rfq.title, "deadline": rfq.deadline, "status": rfq.status,
            "your_quote": {"status": quote.status, "delivery_days": quote.delivery_days,
                           "submitted_on": quote.submitted_on, "reference": quote.supplier_reference},
            "items": [{"material": item.material.number, "description": item.material.description,
                       "quantity": item.quantity, "your_price": prices.get(item.pk)}
                      for item in rfq.items.select_related("material").order_by("id")]}


def rfqs(access: PartnerAccess) -> list[dict]:
    """The requests the supplier was invited to that were sent (open, awarded or closed)."""
    quotes = (SupplierQuote.objects.filter(rfq__company_code=access.company_code, supplier=access.partner)
              .exclude(rfq__status=RequestForQuotation.Status.DRAFT)
              .select_related("rfq").prefetch_related("items").order_by("-rfq__deadline"))
    return [quote_view(q) for q in quotes]


def submit_quote(access: PartnerAccess, rfq_number: str, *, prices: dict[str, Decimal], delivery_days: int,
                 reference: str = "", on: date, user) -> dict:
    """The supplier quotes in person, through the SRM rules (deadline, every item, revisions)."""
    quote = _quote(access, rfq_number)
    materials = {item.material.number: item.material for item in quote.rfq.items.select_related("material")}
    unknown = set(prices) - set(materials)
    if unknown:
        raise PortalError(f"Request {rfq_number} does not ask for {', '.join(sorted(unknown))}.")
    srm.submit_quote(quote, prices={materials[m]: p for m, p in prices.items()}, delivery_days=delivery_days,
                     submitted_on=on, supplier_reference=reference, user=user)
    return quote_view(SupplierQuote.objects.select_related("rfq").get(pk=quote.pk))


# --- supplier: the shared demand outlook ----------------------------------------------------------------------

def share(company_code, supplier, *, active: bool, user, now=None) -> tuple[ForecastShare, bool]:
    """Purchasing switches the outlook on or off for one supplier; returns (share, changed).

    The same state again changes nothing; switching off keeps the record and when it ended.
    """
    if not SupplierRole.objects.filter(business_partner=supplier, company_code=company_code).exists():
        raise PortalError(f"{supplier.number} is not a supplier in company code {company_code.code}.")
    now = now or timezone.now()
    with transaction.atomic():
        locked = ForecastShare.objects.select_for_update()
        current = locked.filter(company_code=company_code, supplier=supplier).first()
        if current is None:
            if not active:
                raise PortalError(f"Nothing is shared with {supplier.number}.")
            current = ForecastShare.objects.create(company_code=company_code, supplier=supplier, agreed_by=user,
                                                   agreed_at=now)
            changed = True
        elif current.active == active:
            changed = False
        else:
            current.active, current.agreed_by = active, user
            current.agreed_at, current.ended_at = (now, None) if active else (current.agreed_at, now)
            current.save()
            changed = True
    if changed:
        logger.info("Demand outlook %s for supplier %s by user %s", "shared" if active else "no longer shared",
                    supplier.number, user.pk)
    return current, changed


def outlook(access: PartnerAccess, *, on: date, months: int = 6) -> dict:
    """What the company expects to buy from the supplier, per material and month, if it agreed to share.

    Per material the supplier supplies (purchase orders, contracts, preferred supplier): what is
    already ordered from this supplier and still open, the purchases MRP has planned (open BUY
    planned orders of the latest runs), and the demand forecast where the material has one. An
    outlook, not an order: only a purchase order commits the company.

    **Fair competition (v0.22.0).** Planned purchases do not name a supplier yet, and the forecast
    is the company's whole demand. Both are shared only for materials where this supplier is the
    preferred supplier (planning parameters) or the only source; for a material with other sources
    the supplier sees what is ordered from it, and ``planned_shared`` is false, so it never learns
    volumes that a competitor may get.
    """
    agreed = ForecastShare.objects.filter(company_code=access.company_code, supplier=access.partner,
                                          active=True).first()
    if agreed is None:
        raise NotShared("The demand outlook is shared only by agreement with purchasing; it is not shared with "
                        "you at the moment.")
    from apps.control.services import _latest_runs
    from apps.planning.risk import _sources

    company = access.company_code
    first = month_start(on)
    periods = [add_months(first, i) for i in range(months)]
    end = add_months(first, months)
    sources = _sources(company, on)
    supplied = sorted(m for m, ids in sources.items() if access.partner.pk in ids)
    preferred = set(MaterialPlanning.objects.filter(plant__company_code=company, preferred_supplier=access.partner)
                    .values_list("material_id", flat=True))
    shared = {m for m in supplied if m in preferred or len(sources[m]) == 1}
    rows: dict[int, dict] = {}

    def row(material) -> dict:
        """The outlook row for a material, created on first use."""
        plans = material.pk in shared
        empty = {f"{p:%Y-%m}": {"ordered": ZERO, "planned": ZERO if plans else None, "forecast": None}
                 for p in periods}
        return rows.setdefault(material.pk, {"material": material.number, "description": material.description,
                                             "planned_shared": plans, "months": empty})

    def bucket(day: date) -> str | None:
        """The month column a date falls in (overdue counts in the first), or None beyond the horizon."""
        return f"{max(month_start(day), first):%Y-%m}" if day < end else None

    for item in (PurchaseOrderItem.objects.filter(order__company_code=company, order__supplier=access.partner,
                                                  order__status=PurchaseOrder.Status.RELEASED,
                                                  material_id__in=supplied)
                 .select_related("material", "order").prefetch_related("confirmations")):
        confirmed = latest_confirmation(item)
        month = bucket(confirmed.delivery_date if confirmed else (item.order.delivery_date or item.order.order_date))
        open_quantity = item.quantity - item.received_quantity
        if month and open_quantity > 0:
            row(item.material)["months"][month]["ordered"] += open_quantity
    for planned in (PlannedOrder.objects.filter(run__in=_latest_runs(company), procurement="BUY",
                                                status=PlannedOrder.Status.OPEN, material_id__in=shared)
                    .select_related("material")):
        month = bucket(planned.due_date)
        if month:
            row(planned.material)["months"][month]["planned"] += planned.quantity
    for period in (ForecastPeriod.objects.filter(item__run__plant__company_code=company,
                                                 item__material_id__in=shared, period__gte=first, period__lt=end)
                   .select_related("item__material").order_by("item__material_id", "period", "-item__run_id")):
        cell = row(period.item.material)["months"][f"{period.period:%Y-%m}"]
        if cell["forecast"] is None:  # the newest run's forecast for that month
            cell["forecast"] = period.quantity
    logger.info("Demand outlook read by supplier %s: %d material(s)", access.partner.number, len(rows))
    return {"supplier": access.partner.number, "from": first, "months": months, "shared_since": agreed.agreed_at,
            "note": "An outlook, not an order: only a purchase order commits the company. Planned purchases "
                    "and forecasts are shown only where you are the preferred or only supplier.",
            "materials": sorted(rows.values(), key=lambda r: r["material"])}


# --- customer: order status ---------------------------------------------------------------------------------

def _sales_orders(access: PartnerAccess):
    return (SalesOrder.objects.filter(company_code=access.company_code, customer=access.partner)
            .prefetch_related("items__material", "deliveries", "deliveries__invoices").order_by("-order_date", "-id"))


def sales_order_view(order: SalesOrder, *, on: date) -> dict:
    """A sales order as its customer sees it: lines, delivered, invoiced, open, late, deliveries, invoices."""
    lines = []
    for item in sorted(order.items.all(), key=lambda i: i.item_number):
        open_quantity = item.open_quantity
        late = bool(order.requested_delivery_date and order.requested_delivery_date < on and open_quantity > 0
                    and order.status not in (SalesOrder.Status.CANCELLED,))
        lines.append({"item_number": item.item_number, "material": item.material.number,
                      "description": item.material.description, "quantity": item.quantity,
                      "delivered": item.delivered_quantity, "invoiced": item.invoiced_quantity,
                      "open": open_quantity, "late": late})
    deliveries = [{"delivery_number": d.delivery_number, "delivery_date": d.delivery_date, "status": d.status}
                  for d in order.deliveries.all()]
    invoices = [{"invoice_number": i.invoice_number, "invoice_date": i.invoice_date, "due_date": i.due_date,
                 "gross_amount": i.gross_amount, "status": i.status}
                for d in order.deliveries.all() for i in d.invoices.all()]
    return {"order_number": order.order_number, "order_date": order.order_date,
            "requested_delivery_date": order.requested_delivery_date, "your_reference": order.customer_reference,
            "status": order.status, "late": any(line["late"] for line in lines), "items": lines,
            "deliveries": deliveries, "invoices": invoices}


def sales_orders(access: PartnerAccess, *, on: date) -> list[dict]:
    """The customer's sales orders, newest first."""
    return [sales_order_view(order, on=on) for order in _sales_orders(access)]


def sales_order(access: PartnerAccess, number: str, *, on: date) -> dict:
    """One of the customer's sales orders (404 for any other)."""
    order = _sales_orders(access).filter(order_number=number).first()
    if order is None:
        raise NotFound(f"Sales order {number} was not found.")
    return sales_order_view(order, on=on)
