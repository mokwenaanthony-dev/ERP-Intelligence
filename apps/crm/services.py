"""CRM business rules (Wave 7a): leads, opportunities, quotations, service tickets, activities, reports.

Every status change is a function here, so the API, the admin, the demo command and the tests all
apply the same rules. A broken rule raises ``CRMError`` (a ``PostingError``), which the API returns
as a 400 and the exception handler logs with the file and line of the rule.

**Idempotency (ADR 0010).** Each transition asked for twice gives the same result as once: qualifying
a qualified lead, converting a converted one (the same opportunity comes back), accepting an accepted
quotation (the same sales order comes back), resolving a resolved ticket. A transition that does not
fit the current status (accepting a rejected quotation) is refused with the reason.

**Personal data (ADR 0012).** Lead contact names, e-mail addresses and phone numbers are never written
to the log: log lines carry document numbers only.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.finance import services as finance
from apps.finance.models import GLAccount
from apps.masterdata.models import BusinessPartner, CustomerRole, Material
from apps.sales import services as sales

from .models import Activity, Campaign, Lead, Opportunity, Quotation, QuotationItem, ServiceTicket

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
CONTINUOUS = 0  # CRM documents are numbered continuously per company code, like sales documents


class CRMError(finance.PostingError):
    """The request breaks a CRM rule and nothing was saved."""


def _next_number(company_code, prefix: str) -> str:
    return finance.next_document_number(company_code, CONTINUOUS, prefix)


def _check_campaign(campaign, company_code):
    if campaign is not None and campaign.company_code_id != company_code.pk:
        raise CRMError(f"Campaign {campaign.code} belongs to another company code.")


def _check_customer(company_code, customer):
    if not CustomerRole.objects.filter(business_partner=customer, company_code=company_code).exists():
        raise CRMError(f"Business partner {customer.number} is not a customer of company code {company_code.code}.")
    if customer.is_blocked:
        raise CRMError(f"Customer {customer.number} is blocked.")


# --- Leads -------------------------------------------------------------------------------------

@transaction.atomic
def create_lead(*, company_code, company_name: str, contact_name: str = "", email: str = "", phone: str = "",
                source: str = Lead.Source.OTHER, campaign: Campaign | None = None, owner=None, user=None) -> Lead:
    """Record a new lead (status NEW)."""
    if not company_name.strip():
        raise CRMError("A lead needs the name of the company.")
    _check_campaign(campaign, company_code)
    lead = Lead.objects.create(
        company_code=company_code, lead_number=_next_number(company_code, "LD"), company_name=company_name.strip(),
        contact_name=contact_name, email=email, phone=phone, source=source, campaign=campaign,
        owner=owner or user, created_by=user, updated_by=user,
    )
    logger.info("Created lead %s (source %s%s)", lead.lead_number, source,
                f", campaign {campaign.code}" if campaign else "")
    return lead


def _locked(model, obj):
    """The row again, locked for this transaction, so two requests change it one after the other."""
    return model.objects.select_for_update().get(pk=obj.pk)


@transaction.atomic
def qualify_lead(lead: Lead, user=None) -> Lead:
    """NEW -> QUALIFIED. Qualifying a qualified lead changes nothing."""
    lead = _locked(Lead, lead)
    if lead.status == Lead.Status.QUALIFIED:
        return lead
    if lead.status != Lead.Status.NEW:
        raise CRMError(f"Only a NEW lead can be qualified (lead {lead.lead_number} is {lead.status}).")
    lead.status, lead.updated_by = Lead.Status.QUALIFIED, user
    lead.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Lead %s qualified", lead.lead_number)
    return lead


@transaction.atomic
def disqualify_lead(lead: Lead, reason: str, user=None) -> Lead:
    """NEW or QUALIFIED -> DISQUALIFIED, with the reason. The same request again changes nothing."""
    lead = _locked(Lead, lead)
    if not reason.strip():
        raise CRMError("Say why the lead is disqualified.")
    if lead.status == Lead.Status.DISQUALIFIED and lead.disqualify_reason == reason.strip():
        return lead
    if lead.status not in (Lead.Status.NEW, Lead.Status.QUALIFIED):
        raise CRMError(f"Lead {lead.lead_number} is {lead.status} and cannot be disqualified.")
    lead.status, lead.disqualify_reason, lead.updated_by = Lead.Status.DISQUALIFIED, reason.strip(), user
    lead.save(update_fields=["status", "disqualify_reason", "updated_by", "updated_at"])
    logger.info("Lead %s disqualified", lead.lead_number)
    return lead


def _receivables_account(company_code) -> GLAccount:
    account = (GLAccount.objects.filter(chart_of_accounts=company_code.chart_of_accounts,
                                        reconciliation_type=GLAccount.ReconciliationType.CUSTOMER)
               .order_by("number").first())
    if account is None:
        raise CRMError(f"Company code {company_code.code} has no receivables reconciliation account.")
    return account


@transaction.atomic
def convert_lead(lead: Lead, *, title: str, expected_close_date: date, expected_value: Decimal = Decimal("0"),
                 customer: BusinessPartner | None = None, user=None) -> Opportunity:
    """Turn a QUALIFIED lead into a customer and an opportunity (stage QUALIFICATION).

    Without ``customer`` a new business partner is created from the lead, with a customer role in the
    lead's company code; with one (the company already exists), that partner is used and given a
    customer role if it has none. Converting a converted lead returns the opportunity made the first time.
    """
    lead = _locked(Lead, lead)
    if lead.status == Lead.Status.CONVERTED:
        return lead.opportunity
    if lead.status != Lead.Status.QUALIFIED:
        raise CRMError(f"Only a QUALIFIED lead can be converted (lead {lead.lead_number} is {lead.status}).")
    if expected_value < 0:
        raise CRMError("The expected value cannot be negative.")
    company_code = lead.company_code
    if customer is None:
        customer = BusinessPartner.objects.create(name=lead.company_name, email=lead.email, phone=lead.phone,
                                                  created_by=user, updated_by=user)
    if customer.is_blocked:
        raise CRMError(f"Business partner {customer.number} is blocked.")
    CustomerRole.objects.get_or_create(
        business_partner=customer, company_code=company_code,
        defaults={"reconciliation_account": _receivables_account(company_code), "created_by": user,
                  "updated_by": user},
    )
    opportunity = Opportunity.objects.create(
        company_code=company_code, opportunity_number=_next_number(company_code, "OP"), customer=customer,
        title=title.strip() or lead.company_name, stage=Opportunity.Stage.QUALIFICATION,
        probability=Opportunity.PROBABILITY["QUALIFICATION"], expected_value=Decimal(expected_value).quantize(CENT),
        expected_close_date=expected_close_date, campaign=lead.campaign, owner=lead.owner or user,
        created_by=user, updated_by=user,
    )
    lead.status, lead.business_partner, lead.opportunity, lead.updated_by = (
        Lead.Status.CONVERTED, customer, opportunity, user)
    lead.save(update_fields=["status", "business_partner", "opportunity", "updated_by", "updated_at"])
    logger.info("Lead %s converted: customer %s, opportunity %s", lead.lead_number, customer.number,
                opportunity.opportunity_number)
    return opportunity


# --- Opportunities -------------------------------------------------------------------------------

@transaction.atomic
def create_opportunity(*, company_code, customer, title: str, expected_close_date: date,
                       expected_value: Decimal = Decimal("0"), stage: str = Opportunity.Stage.PROSPECTING,
                       campaign: Campaign | None = None, owner=None, user=None) -> Opportunity:
    """Open an opportunity for an existing customer."""
    _check_customer(company_code, customer)
    _check_campaign(campaign, company_code)
    if stage not in Opportunity.OPEN_STAGES:
        raise CRMError(f"A new opportunity starts in an open stage ({', '.join(Opportunity.OPEN_STAGES)}).")
    if expected_value < 0:
        raise CRMError("The expected value cannot be negative.")
    opportunity = Opportunity.objects.create(
        company_code=company_code, opportunity_number=_next_number(company_code, "OP"), customer=customer,
        title=title.strip(), stage=stage, probability=Opportunity.PROBABILITY[stage],
        expected_value=Decimal(expected_value).quantize(CENT), expected_close_date=expected_close_date,
        campaign=campaign, owner=owner or user, created_by=user, updated_by=user,
    )
    logger.info("Created opportunity %s for customer %s (%s)", opportunity.opportunity_number, customer.number, stage)
    return opportunity


@transaction.atomic
def move_opportunity(opportunity: Opportunity, stage: str, *, probability: int | None = None,
                     user=None) -> Opportunity:
    """Move an open opportunity to another open stage (forwards or back), with the stage's default
    probability unless one is given. WON comes from accepting a quotation; LOST from ``lose_opportunity``.
    """
    opportunity = _locked(Opportunity, opportunity)
    if not opportunity.is_open:
        raise CRMError(f"Opportunity {opportunity.opportunity_number} is {opportunity.stage}: it is closed.")
    if stage not in Opportunity.OPEN_STAGES:
        raise CRMError(f"Move to an open stage ({', '.join(Opportunity.OPEN_STAGES)}); an opportunity is won by "
                       "accepting its quotation and lost with a reason.")
    probability = Opportunity.PROBABILITY[stage] if probability is None else probability
    if not 0 <= probability <= 100:
        raise CRMError("The probability is a percentage from 0 to 100.")
    if (opportunity.stage, opportunity.probability) == (stage, probability):
        return opportunity
    opportunity.stage, opportunity.probability, opportunity.updated_by = stage, probability, user
    opportunity.save(update_fields=["stage", "probability", "updated_by", "updated_at"])
    logger.info("Opportunity %s moved to %s (%d%%)", opportunity.opportunity_number, stage, probability)
    return opportunity


@transaction.atomic
def lose_opportunity(opportunity: Opportunity, *, reason: str, on: date, user=None) -> Opportunity:
    """Close an open opportunity as LOST, with the reason. The same request again changes nothing."""
    opportunity = _locked(Opportunity, opportunity)
    if not reason.strip():
        raise CRMError("Say why the opportunity was lost.")
    if opportunity.stage == Opportunity.Stage.LOST and opportunity.lost_reason == reason.strip():
        return opportunity
    if not opportunity.is_open:
        raise CRMError(f"Opportunity {opportunity.opportunity_number} is already {opportunity.stage}.")
    opportunity.stage, opportunity.probability = Opportunity.Stage.LOST, 0
    opportunity.lost_reason, opportunity.closed_on, opportunity.updated_by = reason.strip(), on, user
    opportunity.save(update_fields=["stage", "probability", "lost_reason", "closed_on", "updated_by", "updated_at"])
    # Its open quotations are no longer on offer.
    _reject_open(Quotation.objects.filter(opportunity=opportunity), user)
    logger.info("Opportunity %s lost", opportunity.opportunity_number)
    return opportunity


# --- Quotations ----------------------------------------------------------------------------------

def _reject_open(quotations, user):
    """Reject the DRAFT and SENT ones. Saved one by one (not a bulk ``update``), so each change sends
    the signal that writes its outbox event and the graph sees the new status.
    """
    for quotation in quotations.filter(status__in=[Quotation.Status.DRAFT, Quotation.Status.SENT]):
        quotation.status, quotation.updated_by = Quotation.Status.REJECTED, user
        quotation.save(update_fields=["status", "updated_by", "updated_at"])
        logger.info("Quotation %s rejected: no longer needed", quotation.quotation_number)


@dataclass
class QuotationItemInput:
    """One item of a quotation. Leave the price empty to take the price list's on the quotation date."""

    material: Material
    quantity: Decimal
    net_price: Decimal | None = None


def _build_quotation_items(quotation: Quotation, items: list[QuotationItemInput]):
    if not items:
        raise CRMError("A quotation needs at least one item.")
    rows = []
    for number, item in enumerate(items, start=1):
        if not item.material.is_active:
            raise CRMError(f"Material {item.material.number} is not active.")
        if item.quantity is None or item.quantity <= 0:
            raise CRMError("Quantities must be greater than zero.")
        overridden = item.net_price is not None
        if overridden and item.net_price < 0:
            raise CRMError("Prices cannot be negative.")
        price = item.net_price if overridden else sales.price_for(
            item.material, quotation.sales_organization, quotation.quotation_date)
        rows.append(QuotationItem(quotation=quotation, item_number=number * 10, material=item.material,
                                  quantity=item.quantity, net_price=Decimal(price).quantize(CENT),
                                  price_overridden=overridden))
    QuotationItem.objects.bulk_create(rows)


@transaction.atomic
def create_quotation(*, sales_organization, customer, quotation_date: date, valid_until: date,
                     items: list[QuotationItemInput], opportunity: Opportunity | None = None,
                     user=None) -> Quotation:
    """Create a DRAFT quotation, priced from the price list unless a price is given."""
    company_code = sales_organization.company_code
    _check_customer(company_code, customer)
    if valid_until < quotation_date:
        raise CRMError("A quotation cannot expire before its date.")
    if opportunity is not None:
        if opportunity.company_code_id != company_code.pk or opportunity.customer_id != customer.pk:
            raise CRMError(f"Opportunity {opportunity.opportunity_number} is for another customer or company code.")
        if not opportunity.is_open:
            raise CRMError(f"Opportunity {opportunity.opportunity_number} is {opportunity.stage}: it is closed.")
    quotation = Quotation.objects.create(
        company_code=company_code, sales_organization=sales_organization, customer=customer,
        quotation_number=_next_number(company_code, "QT"), opportunity=opportunity, quotation_date=quotation_date,
        valid_until=valid_until, created_by=user, updated_by=user,
    )
    try:
        _build_quotation_items(quotation, items)
    except finance.PostingError as exc:  # e.g. no price on the list: the same message, as a CRM rule
        raise CRMError(str(exc)) from exc
    logger.info("Created quotation %s for customer %s: %d item(s), net %s", quotation.quotation_number,
                customer.number, len(items), quotation.net_total)
    return quotation


@transaction.atomic
def send_quotation(quotation: Quotation, user=None) -> Quotation:
    """DRAFT -> SENT. Its opportunity moves to PROPOSAL (if it was earlier) and takes the quoted value."""
    quotation = _locked(Quotation, quotation)
    if quotation.status == Quotation.Status.SENT:
        return quotation
    if quotation.status != Quotation.Status.DRAFT:
        raise CRMError(f"Only a DRAFT quotation can be sent (quotation {quotation.quotation_number} is "
                       f"{quotation.status}).")
    quotation.status, quotation.updated_by = Quotation.Status.SENT, user
    quotation.save(update_fields=["status", "updated_by", "updated_at"])
    opportunity = quotation.opportunity
    if opportunity is not None and opportunity.is_open:
        opportunity = _locked(Opportunity, opportunity)
        opportunity.expected_value = quotation.net_total
        fields = ["expected_value", "updated_by", "updated_at"]
        if Opportunity.OPEN_STAGES.index(opportunity.stage) < Opportunity.OPEN_STAGES.index("PROPOSAL"):
            opportunity.stage, opportunity.probability = "PROPOSAL", Opportunity.PROBABILITY["PROPOSAL"]
            fields += ["stage", "probability"]
        opportunity.updated_by = user
        opportunity.save(update_fields=fields)
    logger.info("Quotation %s sent", quotation.quotation_number)
    return quotation


@transaction.atomic
def accept_quotation(quotation: Quotation, *, on: date, user=None) -> Quotation:
    """The customer accepts: create the sales order at the quoted prices and win the opportunity.

    Accepting an accepted quotation returns it with the sales order made the first time. An expired
    quotation (``valid_until`` before ``on``) is refused: quote again.
    """
    quotation = _locked(Quotation, quotation)
    if quotation.status == Quotation.Status.ACCEPTED:
        return quotation
    if quotation.status != Quotation.Status.SENT:
        raise CRMError(f"Only a SENT quotation can be accepted (quotation {quotation.quotation_number} is "
                       f"{quotation.status}).")
    if on > quotation.valid_until:
        raise CRMError(f"Quotation {quotation.quotation_number} expired on {quotation.valid_until}.")
    # The quoted price is the agreed price, even if the price list changed since: pass it explicitly.
    items = [sales.OrderItemInput(material=item.material, quantity=item.quantity, net_price=item.net_price)
             for item in quotation.items.select_related("material")]
    order = sales.create_sales_order(
        sales_organization=quotation.sales_organization, customer=quotation.customer, order_date=on,
        customer_reference=quotation.quotation_number, items=items, user=user,
    )
    quotation.status, quotation.sales_order, quotation.updated_by = Quotation.Status.ACCEPTED, order, user
    quotation.save(update_fields=["status", "sales_order", "updated_by", "updated_at"])
    opportunity = quotation.opportunity
    if opportunity is not None:
        opportunity = _locked(Opportunity, opportunity)
        opportunity.stage, opportunity.probability = Opportunity.Stage.WON, 100
        opportunity.expected_value, opportunity.closed_on, opportunity.updated_by = quotation.net_total, on, user
        opportunity.save(update_fields=["stage", "probability", "expected_value", "closed_on", "updated_by",
                                        "updated_at"])
        # The other offers for the same opportunity are no longer needed.
        _reject_open(opportunity.quotations.exclude(pk=quotation.pk), user)
    logger.info("Quotation %s accepted: sales order %s%s", quotation.quotation_number, order.order_number,
                f", opportunity {opportunity.opportunity_number} won" if opportunity else "")
    return quotation


@transaction.atomic
def reject_quotation(quotation: Quotation, user=None) -> Quotation:
    """DRAFT or SENT -> REJECTED. Rejecting a rejected quotation changes nothing."""
    quotation = _locked(Quotation, quotation)
    if quotation.status == Quotation.Status.REJECTED:
        return quotation
    if quotation.status == Quotation.Status.ACCEPTED:
        raise CRMError(f"Quotation {quotation.quotation_number} was accepted (sales order "
                       f"{quotation.sales_order.order_number}); cancel the sales order instead.")
    quotation.status, quotation.updated_by = Quotation.Status.REJECTED, user
    quotation.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Quotation %s rejected", quotation.quotation_number)
    return quotation


# --- Service tickets -------------------------------------------------------------------------------

@transaction.atomic
def open_ticket(*, company_code, customer, subject: str, description: str = "",
                priority: str = ServiceTicket.Priority.MEDIUM, sales_order=None, material=None,
                opened_at: datetime | None = None, owner=None, user=None) -> ServiceTicket:
    """Open a ticket; it is due ``SLA_HOURS[priority]`` after it was opened."""
    _check_customer(company_code, customer)
    if not subject.strip():
        raise CRMError("A ticket needs a subject.")
    if sales_order is not None and (sales_order.customer_id != customer.pk or
                                    sales_order.company_code_id != company_code.pk):
        raise CRMError(f"Sales order {sales_order.order_number} belongs to another customer.")
    opened_at = opened_at or timezone.now()
    ticket = ServiceTicket.objects.create(
        company_code=company_code, ticket_number=_next_number(company_code, "ST"), customer=customer,
        subject=subject.strip(), description=description, priority=priority, opened_at=opened_at,
        due_at=opened_at + timedelta(hours=ServiceTicket.SLA_HOURS[priority]), sales_order=sales_order,
        material=material, owner=owner or user, created_by=user, updated_by=user,
    )
    logger.info("Opened ticket %s for customer %s (%s, due %s)", ticket.ticket_number, customer.number, priority,
                ticket.due_at.isoformat())
    return ticket


@transaction.atomic
def start_ticket(ticket: ServiceTicket, user=None) -> ServiceTicket:
    """OPEN -> IN_PROGRESS. Starting a started ticket changes nothing."""
    ticket = _locked(ServiceTicket, ticket)
    if ticket.status == ServiceTicket.Status.IN_PROGRESS:
        return ticket
    if ticket.status != ServiceTicket.Status.OPEN:
        raise CRMError(f"Ticket {ticket.ticket_number} is {ticket.status}; only an OPEN ticket can be started.")
    ticket.status, ticket.owner, ticket.updated_by = ServiceTicket.Status.IN_PROGRESS, ticket.owner or user, user
    ticket.save(update_fields=["status", "owner", "updated_by", "updated_at"])
    logger.info("Ticket %s in progress", ticket.ticket_number)
    return ticket


@transaction.atomic
def resolve_ticket(ticket: ServiceTicket, *, resolution: str, at: datetime | None = None,
                   user=None) -> ServiceTicket:
    """OPEN or IN_PROGRESS -> RESOLVED, with what was done. Resolving a resolved ticket changes nothing."""
    ticket = _locked(ServiceTicket, ticket)
    if not resolution.strip():
        raise CRMError("Say how the ticket was resolved.")
    if ticket.status == ServiceTicket.Status.RESOLVED:
        return ticket
    if ticket.status not in (ServiceTicket.Status.OPEN, ServiceTicket.Status.IN_PROGRESS):
        raise CRMError(f"Ticket {ticket.ticket_number} is {ticket.status}.")
    ticket.status, ticket.resolution = ServiceTicket.Status.RESOLVED, resolution.strip()
    ticket.resolved_at, ticket.updated_by = at or timezone.now(), user
    ticket.save(update_fields=["status", "resolution", "resolved_at", "updated_by", "updated_at"])
    late = ticket.resolved_at > ticket.due_at
    logger.info("Ticket %s resolved%s", ticket.ticket_number, " after its due time" if late else " within its SLA")
    return ticket


@transaction.atomic
def close_ticket(ticket: ServiceTicket, user=None) -> ServiceTicket:
    """RESOLVED -> CLOSED (the customer agrees). Closing a closed ticket changes nothing."""
    ticket = _locked(ServiceTicket, ticket)
    if ticket.status == ServiceTicket.Status.CLOSED:
        return ticket
    if ticket.status != ServiceTicket.Status.RESOLVED:
        raise CRMError(f"Ticket {ticket.ticket_number} must be RESOLVED before it is closed (it is {ticket.status}).")
    ticket.status, ticket.updated_by = ServiceTicket.Status.CLOSED, user
    ticket.save(update_fields=["status", "updated_by", "updated_at"])
    logger.info("Ticket %s closed", ticket.ticket_number)
    return ticket


@transaction.atomic
def reopen_ticket(ticket: ServiceTicket, user=None) -> ServiceTicket:
    """RESOLVED -> IN_PROGRESS (the answer did not help). The original due time still counts."""
    ticket = _locked(ServiceTicket, ticket)
    if ticket.status == ServiceTicket.Status.IN_PROGRESS:
        return ticket
    if ticket.status != ServiceTicket.Status.RESOLVED:
        raise CRMError(f"Only a RESOLVED ticket can be reopened (ticket {ticket.ticket_number} is {ticket.status}).")
    ticket.status, ticket.resolved_at, ticket.updated_by = ServiceTicket.Status.IN_PROGRESS, None, user
    ticket.save(update_fields=["status", "resolved_at", "updated_by", "updated_at"])
    logger.info("Ticket %s reopened", ticket.ticket_number)
    return ticket


# --- Activities ----------------------------------------------------------------------------------

def log_activity(*, kind: str, subject: str, due_date: date, lead=None, opportunity=None, ticket=None,
                 owner=None, user=None) -> Activity:
    """Plan an activity about exactly one lead, opportunity or ticket."""
    if sum(x is not None for x in (lead, opportunity, ticket)) != 1:
        raise CRMError("An activity is about exactly one lead, opportunity or ticket.")
    if not subject.strip():
        raise CRMError("An activity needs a subject.")
    activity = Activity.objects.create(kind=kind, subject=subject.strip(), due_date=due_date, lead=lead,
                                       opportunity=opportunity, ticket=ticket, owner=owner or user,
                                       created_by=user, updated_by=user)
    logger.info("Planned %s activity %s", kind, activity.pk)
    return activity


def complete_activity(activity: Activity, *, on: date, user=None) -> Activity:
    """Mark an activity done. Completing a completed one keeps its first date."""
    if activity.completed_on is not None:
        return activity
    activity.completed_on, activity.updated_by = on, user
    activity.save(update_fields=["completed_on", "updated_by", "updated_at"])
    logger.info("Completed activity %s", activity.pk)
    return activity


# --- Reports -------------------------------------------------------------------------------------

def pipeline(company_code) -> dict:
    """Open opportunities by stage (count, value, weighted value), the forecast by expected close
    month, and the win rate of closed ones. Weighted value = expected value x probability.
    """
    opportunities = list(Opportunity.objects.filter(company_code=company_code))
    stages, months = [], {}
    for stage in Opportunity.OPEN_STAGES:
        rows = [o for o in opportunities if o.stage == stage]
        stages.append({"stage": stage, "count": len(rows),
                       "value": sum((o.expected_value for o in rows), Decimal("0.00")),
                       "weighted_value": sum((o.weighted_value for o in rows), Decimal("0.00"))})
    for o in opportunities:
        if o.is_open:
            month = o.expected_close_date.strftime("%Y-%m")
            months[month] = months.get(month, Decimal("0.00")) + o.weighted_value
    won = [o for o in opportunities if o.stage == Opportunity.Stage.WON]
    lost = [o for o in opportunities if o.stage == Opportunity.Stage.LOST]
    closed = len(won) + len(lost)
    return {
        "company_code": company_code.code,
        "open": {"count": sum(s["count"] for s in stages), "value": sum((s["value"] for s in stages), Decimal("0")),
                 "weighted_value": sum((s["weighted_value"] for s in stages), Decimal("0"))},
        "stages": stages,
        "forecast_by_month": [{"month": m, "weighted_value": v} for m, v in sorted(months.items())],
        "won": {"count": len(won), "value": sum((o.expected_value for o in won), Decimal("0.00"))},
        "lost": {"count": len(lost)},
        "win_rate": (Decimal(len(won)) / closed * 100).quantize(Decimal("0.1")) if closed else None,
    }


def campaign_performance(company_code) -> list[dict]:
    """Per campaign: leads, converted leads, opportunities, won value, and the return on its budget."""
    campaigns = Campaign.objects.filter(company_code=company_code).annotate(
        leads_count=Count("leads", distinct=True),
        converted=Count("leads", filter=Q(leads__status=Lead.Status.CONVERTED), distinct=True),
        opportunities_count=Count("opportunities", distinct=True),
        won_count=Count("opportunities", filter=Q(opportunities__stage=Opportunity.Stage.WON), distinct=True),
    )
    won_value = dict(Opportunity.objects.filter(company_code=company_code, stage=Opportunity.Stage.WON,
                                                campaign__isnull=False)
                     .values_list("campaign").annotate(total=Sum("expected_value")))
    result = []
    for c in campaigns:
        value = (won_value.get(c.pk) or Decimal("0")).quantize(CENT)
        result.append({
            "code": c.code, "name": c.name, "channel": c.channel, "start_date": c.start_date, "end_date": c.end_date,
            "budget": c.budget, "leads": c.leads_count, "converted_leads": c.converted,
            "opportunities": c.opportunities_count, "won": c.won_count, "won_value": value,
            # Won value per rand spent; None when nothing was spent (no division by zero).
            "return_on_budget": (value / c.budget).quantize(Decimal("0.01")) if c.budget else None,
        })
    return result


def overdue_tickets(company_code, now: datetime | None = None) -> list[ServiceTicket]:
    """Tickets still open or in progress after their due time, the most overdue first."""
    now = now or timezone.now()
    return list(ServiceTicket.objects.select_related("customer").filter(
        company_code=company_code, status__in=[ServiceTicket.Status.OPEN, ServiceTicket.Status.IN_PROGRESS],
        due_at__lt=now).order_by("due_at"))
