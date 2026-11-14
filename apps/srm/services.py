"""SRM business rules (Wave 7b): onboarding, requests for quotation, contracts, supplier evaluation.

Every status change is a function here, used by the API, the demo command and the tests alike. A
broken rule raises ``SRMError`` (a ``PostingError``): a 400 for the caller, a WARNING with file and
line in the log. Purchase orders are always created through ``apps.purchasing.services``, so every
purchasing rule (supplier role, plant, tax code) applies to orders that come from SRM too.

**Idempotency (ADR 0010).** Approving an approved application returns the same supplier; awarding
an awarded request returns the same purchase order; submitting the same prices again changes
nothing; evaluating a supplier for the same period replaces that period's row instead of adding one.

**Personal data (ADR 0013).** Applicants' contact names, e-mail addresses and phone numbers are never
logged: log lines carry application, supplier and document numbers only.
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.finance import services as finance
from apps.finance.models import GLAccount
from apps.masterdata.models import BusinessPartner, Material, SupplierRole
from apps.purchasing import services as purchasing
from apps.purchasing.models import GoodsReceipt, PurchaseOrder, PurchaseOrderItem, SupplierInvoice, SupplierInvoiceItem

from .models import (
    Contract,
    ContractItem,
    ContractRelease,
    QuoteItem,
    RequestForQuotation,
    RFQItem,
    SupplierApplication,
    SupplierEvaluation,
    SupplierQuote,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
TENTH = Decimal("0.1")
CONTINUOUS = 0  # numbered continuously per company code, like purchasing documents


class SRMError(finance.PostingError):
    """The request breaks an SRM rule and nothing was saved."""


def _next_number(company_code, prefix: str) -> str:
    return finance.next_document_number(company_code, CONTINUOUS, prefix)


def _locked(model, obj):
    """The row again, locked for this transaction, so two requests change it one after the other."""
    return model.objects.select_for_update().get(pk=obj.pk)


def _check_supplier(company_code, supplier):
    if not SupplierRole.objects.filter(business_partner=supplier, company_code=company_code).exists():
        raise SRMError(f"Business partner {supplier.number} is not a supplier of company code {company_code.code}.")
    if supplier.is_blocked:
        raise SRMError(f"Supplier {supplier.number} is blocked.")


# --- Onboarding ----------------------------------------------------------------------------------

@transaction.atomic
def submit_application(*, company_code, company_name: str, registration_number: str = "", vat_number: str = "",
                       bbbee_level: int | None = None, supplies: str = "", contact_name: str = "", email: str = "",
                       phone: str = "", user=None) -> SupplierApplication:
    """Record a company's application to become a supplier (status SUBMITTED)."""
    if not company_name.strip():
        raise SRMError("An application needs the name of the company.")
    if bbbee_level is not None and not 1 <= bbbee_level <= 8:
        raise SRMError("The B-BBEE level is 1 to 8 (leave it empty if non-compliant).")
    application = SupplierApplication.objects.create(
        company_code=company_code, application_number=_next_number(company_code, "OB"),
        company_name=company_name.strip(), registration_number=registration_number.strip(),
        vat_number=vat_number.strip(), bbbee_level=bbbee_level, supplies=supplies, contact_name=contact_name,
        email=email, phone=phone, created_by=user, updated_by=user,
    )
    logger.info("Supplier application %s submitted%s", application.application_number,
                f" (B-BBEE level {bbbee_level})" if bbbee_level else "")
    return application


def _payables_account(company_code) -> GLAccount:
    account = (GLAccount.objects.filter(chart_of_accounts=company_code.chart_of_accounts,
                                        reconciliation_type=GLAccount.ReconciliationType.SUPPLIER)
               .order_by("number").first())
    if account is None:
        raise SRMError(f"Company code {company_code.code} has no payables reconciliation account.")
    return account


@transaction.atomic
def approve_application(application: SupplierApplication, user=None) -> SupplierApplication:
    """Due diligence passed: create the supplier (a Business Partner with a supplier role).

    The application must carry a company registration number and a VAT number. A partner with the
    same VAT number already on file is used instead of a duplicate. Approving an approved application
    returns it unchanged.
    """
    application = _locked(SupplierApplication, application)
    if application.status == SupplierApplication.Status.APPROVED:
        return application
    if application.status != SupplierApplication.Status.SUBMITTED:
        raise SRMError(f"Application {application.application_number} is {application.status}.")
    missing = [name for name, value in (("registration number", application.registration_number),
                                        ("VAT number", application.vat_number)) if not value]
    if missing:
        raise SRMError(f"Application {application.application_number} cannot be approved without a "
                       f"{' and a '.join(missing)}.")
    partner = BusinessPartner.objects.filter(vat_number=application.vat_number).first()
    if partner is None:
        partner = BusinessPartner.objects.create(
            name=application.company_name, vat_number=application.vat_number,
            registration_number=application.registration_number, email=application.email, phone=application.phone,
            created_by=user, updated_by=user,
        )
    elif partner.is_blocked:
        raise SRMError(f"Business partner {partner.number} with VAT number {application.vat_number} is blocked.")
    SupplierRole.objects.get_or_create(
        business_partner=partner, company_code=application.company_code,
        defaults={"reconciliation_account": _payables_account(application.company_code), "created_by": user,
                  "updated_by": user},
    )
    application.status, application.business_partner, application.updated_by = (
        SupplierApplication.Status.APPROVED, partner, user)
    application.save(update_fields=["status", "business_partner", "updated_by", "updated_at"])
    logger.info("Supplier application %s approved: supplier %s", application.application_number, partner.number)
    return application


@transaction.atomic
def reject_application(application: SupplierApplication, reason: str, user=None) -> SupplierApplication:
    """SUBMITTED -> REJECTED, with the reason. The same request again changes nothing."""
    application = _locked(SupplierApplication, application)
    if not reason.strip():
        raise SRMError("Say why the application is rejected.")
    if application.status == SupplierApplication.Status.REJECTED and application.rejection_reason == reason.strip():
        return application
    if application.status != SupplierApplication.Status.SUBMITTED:
        raise SRMError(f"Application {application.application_number} is {application.status}.")
    application.status, application.rejection_reason, application.updated_by = (
        SupplierApplication.Status.REJECTED, reason.strip(), user)
    application.save(update_fields=["status", "rejection_reason", "updated_by", "updated_at"])
    logger.info("Supplier application %s rejected", application.application_number)
    return application


# --- Requests for quotation ------------------------------------------------------------------------

@dataclass
class RFQItemInput:
    """One material and quantity to ask for."""

    material: Material
    quantity: Decimal


@transaction.atomic
def create_rfq(*, purchasing_organization, title: str, deadline: date, items: list[RFQItemInput],
               suppliers: list[BusinessPartner], user=None) -> RequestForQuotation:
    """A DRAFT request for quotation, with its items and the suppliers invited (at least two, so
    there is something to compare)."""
    company_code = purchasing_organization.company_code
    if not items:
        raise SRMError("A request for quotation needs at least one item.")
    if len({s.pk for s in suppliers}) < 2:
        raise SRMError("Invite at least two different suppliers, so their prices can be compared.")
    for supplier in suppliers:
        _check_supplier(company_code, supplier)
    materials = [item.material.pk for item in items]
    if len(set(materials)) != len(materials):
        raise SRMError("Each material may appear only once in a request for quotation.")
    rfq = RequestForQuotation.objects.create(
        company_code=company_code, purchasing_organization=purchasing_organization,
        rfq_number=_next_number(company_code, "RQ"), title=title.strip(), deadline=deadline,
        created_by=user, updated_by=user,
    )
    for number, item in enumerate(items, start=1):
        if item.quantity is None or item.quantity <= 0:
            raise SRMError("Quantities must be greater than zero.")
        if not item.material.is_active:
            raise SRMError(f"Material {item.material.number} is not active.")
        RFQItem.objects.create(rfq=rfq, item_number=number * 10, material=item.material, quantity=item.quantity)
    for supplier in {s.pk: s for s in suppliers}.values():
        SupplierQuote.objects.create(rfq=rfq, supplier=supplier, created_by=user, updated_by=user)
    logger.info("Created request for quotation %s: %d item(s), %d supplier(s) invited", rfq.rfq_number,
                len(items), len({s.pk for s in suppliers}))
    return rfq


@transaction.atomic
def send_rfq(rfq: RequestForQuotation, user=None) -> RequestForQuotation:
    """DRAFT -> SENT: the invited suppliers may now quote until the deadline."""
    rfq = _locked(RequestForQuotation, rfq)
    if rfq.status == RequestForQuotation.Status.SENT:
        return rfq
    if rfq.status != RequestForQuotation.Status.DRAFT:
        raise SRMError(f"Only a DRAFT request can be sent ({rfq.rfq_number} is {rfq.status}).")
    rfq.status, rfq.updated_by = RequestForQuotation.Status.SENT, user
    rfq.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Request for quotation %s sent", rfq.rfq_number)
    return rfq


@transaction.atomic
def submit_quote(quote: SupplierQuote, *, prices: dict, delivery_days: int, submitted_on: date,
                 supplier_reference: str = "", user=None) -> SupplierQuote:
    """Record a supplier's prices (``{material: net_price}`` for every item) until the deadline.

    A supplier may revise its quote before the deadline: the new prices replace the old ones. The
    same prices again change nothing.
    """
    quote = _locked(SupplierQuote, quote)
    rfq = quote.rfq
    if rfq.status != RequestForQuotation.Status.SENT:
        raise SRMError(f"Request {rfq.rfq_number} is {rfq.status}: it does not take quotes.")
    if submitted_on > rfq.deadline:
        raise SRMError(f"The deadline of request {rfq.rfq_number} was {rfq.deadline}.")
    if delivery_days is None or delivery_days < 0:
        raise SRMError("Give the delivery time in days.")
    items = {item.material_id: item for item in rfq.items.all()}
    given = {getattr(material, "pk", material): Decimal(price) for material, price in prices.items()}
    if set(given) != set(items):
        raise SRMError(f"Quote a price for every item of request {rfq.rfq_number}, and nothing else.")
    if any(price < 0 for price in given.values()):
        raise SRMError("Prices cannot be negative.")
    current = {qi.rfq_item.material_id: qi.net_price for qi in quote.items.select_related("rfq_item")}
    unchanged = (quote.status == SupplierQuote.Status.SUBMITTED and current == given
                 and quote.delivery_days == delivery_days)
    if unchanged:
        return quote
    quote.items.all().delete()
    QuoteItem.objects.bulk_create([QuoteItem(quote=quote, rfq_item=items[material], net_price=price.quantize(CENT))
                                   for material, price in given.items()])
    quote.status, quote.delivery_days, quote.submitted_on = SupplierQuote.Status.SUBMITTED, delivery_days, submitted_on
    quote.supplier_reference, quote.updated_by = supplier_reference, user
    quote.save(update_fields=["status", "delivery_days", "submitted_on", "supplier_reference", "updated_by",
                              "updated_at"])
    logger.info("Quote of supplier %s for %s %s: total %s, %d day(s)", quote.supplier.number, rfq.rfq_number,
                "revised" if current else "received", quote_total(quote), delivery_days)
    return quote


def quote_total(quote: SupplierQuote) -> Decimal:
    """Sum of quantity x quoted price over the request's items."""
    total = sum((qi.rfq_item.quantity * qi.net_price for qi in quote.items.select_related("rfq_item")),
                Decimal("0"))
    return total.quantize(CENT)


def compare_quotes(rfq: RequestForQuotation) -> dict:
    """The quotes side by side: per supplier its total and delivery time, ranked cheapest first; per
    item the lowest price and who offered it."""
    quotes = [q for q in rfq.quotes.select_related("supplier").prefetch_related("items__rfq_item")
              if q.status != SupplierQuote.Status.INVITED]
    ranking = sorted(({"supplier": q.supplier.number, "name": q.supplier.name, "status": q.status,
                       "total": quote_total(q), "delivery_days": q.delivery_days, "quote_id": q.pk} for q in quotes),
                     key=lambda row: (row["total"], row["delivery_days"]))
    items = []
    for item in rfq.items.select_related("material"):
        offers = sorted(((qi.net_price, q.supplier.number) for q in quotes for qi in q.items.all()
                         if qi.rfq_item_id == item.pk))
        items.append({"item_number": item.item_number, "material": item.material.number,
                      "quantity": item.quantity, "lowest_price": offers[0][0] if offers else None,
                      "lowest_from": offers[0][1] if offers else None, "offers": len(offers)})
    invited = rfq.quotes.count()
    return {"rfq": rfq.rfq_number, "status": rfq.status, "deadline": rfq.deadline, "invited": invited,
            "answered": len(quotes), "ranking": ranking, "items": items}


@transaction.atomic
def award_rfq(rfq: RequestForQuotation, quote: SupplierQuote, *, on: date, user=None) -> RequestForQuotation:
    """Give the business to one supplier: a purchase order at its quoted prices, delivery date from
    its delivery time. The other quotes are LOST. Awarding an awarded request returns it unchanged.
    """
    rfq = _locked(RequestForQuotation, rfq)
    if rfq.status == RequestForQuotation.Status.AWARDED:
        return rfq
    if rfq.status != RequestForQuotation.Status.SENT:
        raise SRMError(f"Only a SENT request can be awarded ({rfq.rfq_number} is {rfq.status}).")
    quote = _locked(SupplierQuote, quote)
    if quote.rfq_id != rfq.pk:
        raise SRMError(f"That quote belongs to another request, not {rfq.rfq_number}.")
    if quote.status != SupplierQuote.Status.SUBMITTED:
        raise SRMError(f"Supplier {quote.supplier.number} has not quoted for {rfq.rfq_number}.")
    prices = {qi.rfq_item_id: qi.net_price for qi in quote.items.all()}
    items = [purchasing.OrderItemInput(material=item.material, quantity=item.quantity, net_price=prices[item.pk])
             for item in rfq.items.select_related("material")]
    order = purchasing.create_purchase_order(
        purchasing_organization=rfq.purchasing_organization, supplier=quote.supplier, order_date=on,
        delivery_date=on + timedelta(days=quote.delivery_days or 0),
        supplier_reference=(quote.supplier_reference or rfq.rfq_number)[:35], items=items, user=user,
    )
    for other in rfq.quotes.exclude(pk=quote.pk):
        other.status, other.updated_by = SupplierQuote.Status.LOST, user
        other.save(update_fields=["status", "updated_by", "updated_at"])
    quote.status, quote.updated_by = SupplierQuote.Status.AWARDED, user
    quote.save(update_fields=["status", "updated_by", "updated_at"])
    rfq.status, rfq.purchase_order, rfq.updated_by = RequestForQuotation.Status.AWARDED, order, user
    rfq.save(update_fields=["status", "purchase_order", "updated_by", "updated_at"])
    logger.info("Request %s awarded to supplier %s: purchase order %s", rfq.rfq_number, quote.supplier.number,
                order.order_number)
    return rfq


@transaction.atomic
def cancel_rfq(rfq: RequestForQuotation, user=None) -> RequestForQuotation:
    """DRAFT or SENT -> CANCELLED (nothing will be bought). Cancelling again changes nothing."""
    rfq = _locked(RequestForQuotation, rfq)
    if rfq.status == RequestForQuotation.Status.CANCELLED:
        return rfq
    if rfq.status == RequestForQuotation.Status.AWARDED:
        raise SRMError(f"Request {rfq.rfq_number} was awarded (purchase order {rfq.purchase_order.order_number}); "
                       "cancel the purchase order instead.")
    rfq.status, rfq.updated_by = RequestForQuotation.Status.CANCELLED, user
    rfq.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Request for quotation %s cancelled", rfq.rfq_number)
    return rfq


# --- Contracts ---------------------------------------------------------------------------------

@dataclass
class ContractItemInput:
    """One material on a contract: the agreed price and the quantity it covers."""

    material: Material
    net_price: Decimal
    target_quantity: Decimal


@transaction.atomic
def create_contract(*, purchasing_organization, supplier, valid_from: date, valid_to: date,
                    items: list[ContractItemInput], user=None) -> Contract:
    """An ACTIVE outline agreement with a supplier."""
    company_code = purchasing_organization.company_code
    _check_supplier(company_code, supplier)
    if valid_to < valid_from:
        raise SRMError("A contract cannot end before it starts.")
    if not items:
        raise SRMError("A contract needs at least one item.")
    materials = [item.material.pk for item in items]
    if len(set(materials)) != len(materials):
        raise SRMError("Each material may appear only once on a contract.")
    contract = Contract.objects.create(
        company_code=company_code, purchasing_organization=purchasing_organization,
        contract_number=_next_number(company_code, "CT"), supplier=supplier, valid_from=valid_from,
        valid_to=valid_to, created_by=user, updated_by=user,
    )
    for number, item in enumerate(items, start=1):
        if item.net_price < 0 or item.target_quantity <= 0:
            raise SRMError("Prices cannot be negative and target quantities must be greater than zero.")
        ContractItem.objects.create(contract=contract, item_number=number * 10, material=item.material,
                                    net_price=Decimal(item.net_price).quantize(CENT),
                                    target_quantity=item.target_quantity)
    logger.info("Created contract %s with supplier %s: %d item(s), %s to %s", contract.contract_number,
                supplier.number, len(items), valid_from, valid_to)
    return contract


def released_quantities(contract: Contract) -> dict:
    """Quantity already ordered per material through releases (cancelled orders do not count)."""
    rows = (PurchaseOrderItem.objects
            .filter(order__contract_release__contract=contract)
            .exclude(order__status=PurchaseOrder.Status.CANCELLED)
            .values("material_id").annotate(total=Sum("quantity")))
    return {row["material_id"]: row["total"] for row in rows}


@transaction.atomic
def release_order(contract: Contract, *, order_date: date, items: list[tuple], delivery_date: date | None = None,
                  user=None) -> PurchaseOrder:
    """A purchase order against the contract, at the contract prices, within its validity and its
    target quantities. ``items`` are ``(material, quantity)`` pairs."""
    contract = _locked(Contract, contract)  # one release at a time, so two cannot both use the last units
    if contract.status != Contract.Status.ACTIVE:
        raise SRMError(f"Contract {contract.contract_number} is {contract.status}.")
    if not contract.valid_from <= order_date <= contract.valid_to:
        raise SRMError(f"Contract {contract.contract_number} is valid from {contract.valid_from} to "
                       f"{contract.valid_to}, not on {order_date}.")
    if not items:
        raise SRMError("A release needs at least one item.")
    agreed = {item.material_id: item for item in contract.items.select_related("material")}
    released = released_quantities(contract)
    order_items = []
    for material, quantity in items:
        line = agreed.get(material.pk)
        if line is None:
            raise SRMError(f"Material {material.number} is not on contract {contract.contract_number}.")
        if quantity is None or quantity <= 0:
            raise SRMError("Quantities must be greater than zero.")
        left = line.target_quantity - released.get(material.pk, Decimal("0"))
        if quantity > left:
            raise SRMError(f"Only {left} of {material.number} is left on contract {contract.contract_number} "
                           f"(target {line.target_quantity}).")
        order_items.append(purchasing.OrderItemInput(material=material, quantity=quantity, net_price=line.net_price))
    order = purchasing.create_purchase_order(
        purchasing_organization=contract.purchasing_organization, supplier=contract.supplier, order_date=order_date,
        delivery_date=delivery_date, supplier_reference=contract.contract_number, items=order_items, user=user,
    )
    ContractRelease.objects.create(contract=contract, purchase_order=order)
    logger.info("Released purchase order %s against contract %s", order.order_number, contract.contract_number)
    return order


@transaction.atomic
def cancel_contract(contract: Contract, user=None) -> Contract:
    """ACTIVE -> CANCELLED: no more releases. Orders already released stay as they are."""
    contract = _locked(Contract, contract)
    if contract.status == Contract.Status.CANCELLED:
        return contract
    contract.status, contract.updated_by = Contract.Status.CANCELLED, user
    contract.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Contract %s cancelled", contract.contract_number)
    return contract


# --- Supplier evaluation -------------------------------------------------------------------------

# Weights of the measures in the overall score; a measure with nothing to measure is left out and
# the others are weighted up, so a new supplier is not punished for having no invoices yet.
WEIGHTS = {"on_time_rate": Decimal("0.40"), "in_full_rate": Decimal("0.20"), "price_rate": Decimal("0.20"),
           "quality": Decimal("0.20")}
GRADES = ((Decimal("90"), "A"), (Decimal("75"), "B"), (Decimal("60"), "C"))  # below 60: D


def _percent(part, whole) -> Decimal | None:
    return (Decimal(part) * 100 / Decimal(whole)).quantize(TENTH) if whole else None


def measure_supplier(company_code, supplier, period_start: date, period_end: date) -> dict:
    """The measured part of an evaluation, from the supplier's documents in the period:

    * on time: posted goods receipts dated on or before their order's delivery date (orders without
      a delivery date are not counted);
    * in full: per order item received in the period, the received share of the ordered quantity,
      capped at 100 % (over-delivery is not rewarded), averaged;
    * price: posted invoice items at exactly the order price (no price difference).
    """
    receipts = list(GoodsReceipt.objects.filter(
        purchase_order__supplier=supplier, company_code=company_code, status=GoodsReceipt.Status.POSTED,
        receipt_date__range=(period_start, period_end), purchase_order__delivery_date__isnull=False,
    ).select_related("purchase_order"))
    on_time = sum(1 for r in receipts if r.receipt_date <= r.purchase_order.delivery_date)

    order_items = PurchaseOrderItem.objects.filter(
        order__supplier=supplier, order__company_code=company_code,
        receipt_items__receipt__status=GoodsReceipt.Status.POSTED,
        receipt_items__receipt__receipt_date__range=(period_start, period_end),
    ).distinct()
    shares = []
    for item in order_items:
        received = item.receipt_items.filter(receipt__status=GoodsReceipt.Status.POSTED) \
            .aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        shares.append(min(received / item.quantity, Decimal("1")))

    invoice_items = list(SupplierInvoiceItem.objects.filter(
        invoice__supplier=supplier, invoice__company_code=company_code,
        invoice__status=SupplierInvoice.Status.POSTED, invoice__invoice_date__range=(period_start, period_end),
    ).values_list("price_difference", flat=True))
    exact = sum(1 for difference in invoice_items if difference == 0)

    return {
        "on_time_rate": _percent(on_time, len(receipts)),
        "in_full_rate": (sum(shares) * 100 / len(shares)).quantize(TENTH) if shares else None,
        "price_rate": _percent(exact, len(invoice_items)),
        "receipts": len(receipts), "order_items": len(shares), "invoice_items": len(invoice_items),
    }


def grade_for(score: Decimal) -> str:
    """A (90 and above), B (75), C (60), D (below 60)."""
    return next((grade for floor, grade in GRADES if score >= floor), "D")


@transaction.atomic
def evaluate_supplier(*, company_code, supplier, period_start: date, period_end: date, quality_score: int,
                      evaluated_on: date, user=None) -> SupplierEvaluation:
    """Measure the supplier for the period, combine with the quality score (1-5) and grade it.
    Evaluating the same period again replaces its row."""
    _check_supplier(company_code, supplier)
    if period_end < period_start:
        raise SRMError("The period cannot end before it starts.")
    if not 1 <= quality_score <= 5:
        raise SRMError("The quality score is 1 (poor) to 5 (excellent).")
    measured = measure_supplier(company_code, supplier, period_start, period_end)
    scores = {name: measured[name] for name in ("on_time_rate", "in_full_rate", "price_rate")}
    scores["quality"] = Decimal((quality_score - 1) * 25)  # 1 -> 0 %, 3 -> 50 %, 5 -> 100 %
    used = {name: value for name, value in scores.items() if value is not None}
    weight = sum(WEIGHTS[name] for name in used)
    overall = (sum(value * WEIGHTS[name] for name, value in used.items()) / weight).quantize(TENTH)
    evaluation, created = SupplierEvaluation.objects.update_or_create(
        company_code=company_code, supplier=supplier, period_start=period_start, period_end=period_end,
        defaults={"on_time_rate": scores["on_time_rate"], "in_full_rate": scores["in_full_rate"],
                  "price_rate": scores["price_rate"], "quality_score": quality_score, "overall_score": overall,
                  "grade": grade_for(overall), "evaluated_on": evaluated_on, "evaluated_by": user},
    )
    logger.info("Supplier %s %s for %s..%s: %s (grade %s) from %d receipt(s), %d invoice item(s)", supplier.number,
                "evaluated" if created else "re-evaluated", period_start, period_end, overall, evaluation.grade,
                measured["receipts"], measured["invoice_items"])
    return evaluation


def scorecard(company_code) -> list[dict]:
    """Every supplier of the company code with its latest evaluation (or none yet)."""
    suppliers = BusinessPartner.objects.filter(supplier_roles__company_code=company_code).order_by("number")
    latest = {}
    for evaluation in SupplierEvaluation.objects.filter(company_code=company_code).order_by("period_end"):
        latest[evaluation.supplier_id] = evaluation  # the last one per supplier wins
    rows = []
    for supplier in suppliers:
        e = latest.get(supplier.pk)
        rows.append({"supplier": supplier.number, "name": supplier.name,
                     **({"period_start": e.period_start, "period_end": e.period_end, "on_time_rate": e.on_time_rate,
                         "in_full_rate": e.in_full_rate, "price_rate": e.price_rate,
                         "quality_score": e.quality_score, "overall_score": e.overall_score, "grade": e.grade}
                        if e else {"grade": None})})
    return rows
