"""Customer relationship management (Wave 7a): from a first contact to a sales order, and after-sales service.

    Campaign -> Lead -> (convert) -> Business Partner (customer role) + Opportunity
    Opportunity -> Quotation -> (accept) -> Sales order (apps.sales) ; Opportunity WON
    Customer -> Service ticket (priority, SLA due time) ; Activities (calls, meetings, tasks)

* ``Campaign`` - a marketing effort with a budget; leads and opportunities point to it, so its return
  (won value against budget) can be measured.
* ``Lead`` - an interested company not yet a customer. Its contact details are **personal data**: they
  never enter logs or the knowledge graph (ADR 0012).
* ``Opportunity`` - a potential sale to a customer, moving through stages with a win probability.
* ``Quotation`` / ``QuotationItem`` - a priced offer, valid until a date. Accepting it creates the sales
  order through ``apps.sales.services``, at the quoted prices.
* ``ServiceTicket`` - a customer's request or complaint, with a priority and the time it is due by.
* ``Activity`` - a call, meeting, e-mail or task about a lead, an opportunity or a ticket.

CRM documents are numbered continuously per company code (LD, OP, QT, ST), like sales documents. Every
change of status goes through ``apps.crm.services``, where the rules and the idempotency live.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q, Sum

from apps.common.models import AuditedModel

campaign_code = RegexValidator(r"^[A-Z0-9][A-Z0-9-]{1,11}$", "Use 2-12 upper-case letters, digits or hyphens.")


class Campaign(AuditedModel):
    """A marketing effort (an e-mail drive, a trade show) with a budget and a period."""

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "E-mail"
        EVENT = "EVENT", "Event or trade show"
        WEB = "WEB", "Website"
        SOCIAL = "SOCIAL", "Social media"
        PRINT = "PRINT", "Print"
        OTHER = "OTHER", "Other"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=12, validators=[campaign_code])
    name = models.CharField(max_length=100)
    channel = models.CharField(max_length=6, choices=Channel.choices, default=Channel.OTHER)
    start_date = models.DateField()
    end_date = models.DateField()
    budget = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["company_code__code", "-start_date", "code"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "code"], name="unique_campaign_code"),
            models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="campaign_ends_after_start"),
            models.CheckConstraint(condition=Q(budget__gte=0), name="campaign_budget_non_negative"),
        ]

    def __str__(self):
        return f"{self.company_code.code}/{self.code} {self.name}"


class Lead(AuditedModel):
    """An interested company that is not a customer yet. Converting it creates the customer and an
    opportunity; the lead keeps links to both.
    """

    class Source(models.TextChoices):
        WEBSITE = "WEBSITE", "Website"
        REFERRAL = "REFERRAL", "Referral"
        CAMPAIGN = "CAMPAIGN", "Campaign"
        TRADE_SHOW = "TRADE_SHOW", "Trade show"
        COLD_CALL = "COLD_CALL", "Cold call"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        QUALIFIED = "QUALIFIED", "Qualified"  # worth pursuing
        DISQUALIFIED = "DISQUALIFIED", "Disqualified"  # not a fit; the reason is kept
        CONVERTED = "CONVERTED", "Converted"  # now a customer with an opportunity

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    lead_number = models.CharField(max_length=12)
    company_name = models.CharField(max_length=200)
    # Personal data (POPIA): kept for the sales team, never logged or projected into the graph.
    contact_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.OTHER)
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, null=True, blank=True, related_name="leads")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    disqualify_reason = models.CharField(max_length=200, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    business_partner = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, null=True,
                                         blank=True, related_name="+", help_text="Set when converted.")
    opportunity = models.OneToOneField("Opportunity", on_delete=models.PROTECT, null=True, blank=True,
                                       related_name="lead", help_text="Set when converted.")

    class Meta:
        ordering = ["company_code__code", "-lead_number"]
        constraints = [models.UniqueConstraint(fields=["company_code", "lead_number"], name="unique_lead_number")]

    def __str__(self):
        return f"{self.company_code.code}/{self.lead_number}"


class Opportunity(AuditedModel):
    """A potential sale to a customer. Open stages carry a default win probability; WON and LOST close it."""

    class Stage(models.TextChoices):
        PROSPECTING = "PROSPECTING", "Prospecting"
        QUALIFICATION = "QUALIFICATION", "Qualification"
        PROPOSAL = "PROPOSAL", "Proposal"
        NEGOTIATION = "NEGOTIATION", "Negotiation"
        WON = "WON", "Won"
        LOST = "LOST", "Lost"

    # Default win probability per stage (percent); a sales rep may set another while the stage is open.
    PROBABILITY = {"PROSPECTING": 10, "QUALIFICATION": 25, "PROPOSAL": 50, "NEGOTIATION": 75, "WON": 100, "LOST": 0}
    OPEN_STAGES = ("PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION")

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    opportunity_number = models.CharField(max_length=12)
    customer = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="opportunities")
    title = models.CharField(max_length=200)
    stage = models.CharField(max_length=13, choices=Stage.choices, default=Stage.PROSPECTING)
    probability = models.PositiveSmallIntegerField(default=10, validators=[MaxValueValidator(100)])
    expected_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"),
                                         help_text="Net value expected; set from the quotation once one is sent.")
    expected_close_date = models.DateField()
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="opportunities")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    closed_on = models.DateField(null=True, blank=True)
    lost_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["company_code__code", "-opportunity_number"]
        verbose_name_plural = "opportunities"
        constraints = [
            models.UniqueConstraint(fields=["company_code", "opportunity_number"], name="unique_opportunity_number"),
            models.CheckConstraint(condition=Q(expected_value__gte=0), name="opportunity_value_non_negative"),
        ]

    @property
    def is_open(self) -> bool:
        """True until the opportunity is won or lost."""
        return self.stage in self.OPEN_STAGES

    @property
    def weighted_value(self) -> Decimal:
        """Expected value times the win probability: what the pipeline forecast counts."""
        return (self.expected_value * self.probability / Decimal(100)).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.company_code.code}/{self.opportunity_number} {self.title}"


class Quotation(AuditedModel):
    """A priced offer to a customer, valid until a date. Accepted, it becomes a sales order."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"  # being prepared; items can change
        SENT = "SENT", "Sent"  # with the customer
        ACCEPTED = "ACCEPTED", "Accepted"  # the sales order exists
        REJECTED = "REJECTED", "Rejected"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    sales_organization = models.ForeignKey("organization.SalesOrganization", on_delete=models.PROTECT,
                                           related_name="+")
    quotation_number = models.CharField(max_length=12)
    customer = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="quotations")
    opportunity = models.ForeignKey(Opportunity, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="quotations")
    quotation_date = models.DateField()
    valid_until = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    sales_order = models.OneToOneField("sales.SalesOrder", on_delete=models.PROTECT, null=True, blank=True,
                                       related_name="quotation")

    class Meta:
        ordering = ["company_code__code", "-quotation_number"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "quotation_number"], name="unique_quotation_number"),
            models.CheckConstraint(condition=Q(valid_until__gte=F("quotation_date")),
                                   name="quotation_valid_after_date"),
        ]

    @property
    def net_total(self) -> Decimal:
        """Sum of quantity x net price over the items."""
        total = self.items.aggregate(total=Sum(F("quantity") * F("net_price")))["total"]
        return Decimal(total or 0).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.company_code.code}/{self.quotation_number}"


class QuotationItem(models.Model):
    """One material on a quotation, at the price offered (from the price list, or set by the sales rep)."""

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="items")
    item_number = models.PositiveIntegerField()
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    net_price = models.DecimalField(max_digits=18, decimal_places=2)
    price_overridden = models.BooleanField(default=False)

    class Meta:
        ordering = ["quotation", "item_number"]
        constraints = [
            models.UniqueConstraint(fields=["quotation", "item_number"], name="unique_quotation_item"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="quotation_quantity_positive"),
            models.CheckConstraint(condition=Q(net_price__gte=0), name="quotation_price_non_negative"),
        ]

    @property
    def net_amount(self) -> Decimal:
        """Quantity x net price, to the cent."""
        return (self.quantity * self.net_price).quantize(Decimal("0.01"))


class ServiceTicket(AuditedModel):
    """A customer's request or complaint. Its priority sets the time it must be resolved by (SLA)."""

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    # Hours to resolve, per priority (the service level agreement).
    SLA_HOURS = {"LOW": 72, "MEDIUM": 24, "HIGH": 8, "URGENT": 4}

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RESOLVED = "RESOLVED", "Resolved"  # answered; the customer may still reopen it
        CLOSED = "CLOSED", "Closed"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    ticket_number = models.CharField(max_length=12)
    customer = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="tickets")
    subject = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=6, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=11, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField()
    due_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True)
    sales_order = models.ForeignKey("sales.SalesOrder", on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="tickets")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="+")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")

    class Meta:
        ordering = ["company_code__code", "-ticket_number"]
        constraints = [models.UniqueConstraint(fields=["company_code", "ticket_number"], name="unique_ticket_number")]

    def __str__(self):
        return f"{self.company_code.code}/{self.ticket_number} {self.subject}"


class Activity(AuditedModel):
    """A call, meeting, e-mail or task about exactly one lead, opportunity or service ticket."""

    class Kind(models.TextChoices):
        CALL = "CALL", "Call"
        MEETING = "MEETING", "Meeting"
        EMAIL = "EMAIL", "E-mail"
        TASK = "TASK", "Task"

    kind = models.CharField(max_length=7, choices=Kind.choices)
    subject = models.CharField(max_length=200)
    due_date = models.DateField()
    completed_on = models.DateField(null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, null=True, blank=True, related_name="activities")
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="activities")
    ticket = models.ForeignKey(ServiceTicket, on_delete=models.CASCADE, null=True, blank=True,
                               related_name="activities")

    class Meta:
        ordering = ["due_date", "id"]
        verbose_name_plural = "activities"
        constraints = [
            # Exactly one of lead, opportunity and ticket: an activity is always about one thing.
            models.CheckConstraint(
                condition=(Q(lead__isnull=False, opportunity__isnull=True, ticket__isnull=True)
                           | Q(lead__isnull=True, opportunity__isnull=False, ticket__isnull=True)
                           | Q(lead__isnull=True, opportunity__isnull=True, ticket__isnull=False)),
                name="activity_about_one_thing",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.subject} ({self.due_date})"
