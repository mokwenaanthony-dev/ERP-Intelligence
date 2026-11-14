"""Serializers for CRM: validate API input and shape API output.

Input serializers check types and required fields; every business rule runs in ``services.py`` and
comes back as a 400 with the rule's message. Output serializers are read-only views of the records.
"""
from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import Role
from apps.finance.services import PostingError
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import CompanyCode, SalesOrganization
from apps.sales.models import SalesOrder

from . import services
from .models import Activity, Campaign, Lead, Opportunity, Quotation, QuotationItem, ServiceTicket

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


def rule_error(exc):
    """A broken business rule as a 400 response body."""
    return serializers.ValidationError({"non_field_errors": [str(exc)]})


def run(func, *args, **kwargs):
    """Call a service; a broken rule becomes a 400 (the cause is kept, so the log shows file and line)."""
    try:
        return func(*args, **kwargs)
    except PostingError as exc:
        raise rule_error(exc) from exc


class _CompanyField(serializers.SlugRelatedField):
    def __init__(self, **kwargs):
        super().__init__(slug_field="code", queryset=CompanyCode.objects.all(), **kwargs)


# --- Campaigns -------------------------------------------------------------------------------------

class CampaignSerializer(serializers.ModelSerializer):
    company_code = _CompanyField()

    class Meta:
        model = Campaign
        fields = ["id", "company_code", "code", "name", "channel", "start_date", "end_date", "budget", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "Must be on or after the start date."})
        if attrs.get("budget") is not None and attrs["budget"] < 0:
            raise serializers.ValidationError({"budget": "Cannot be negative."})
        return attrs


# --- Leads -----------------------------------------------------------------------------------------

class LeadSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code", read_only=True)
    campaign = serializers.CharField(source="campaign.code", read_only=True, default=None)
    business_partner = serializers.CharField(source="business_partner.number", read_only=True, default=None)
    opportunity = serializers.CharField(source="opportunity.opportunity_number", read_only=True, default=None)
    owner = serializers.CharField(source="owner.username", read_only=True, default=None)

    class Meta:
        model = Lead
        fields = ["id", "lead_number", "company_code", "company_name", "contact_name", "email", "phone", "source",
                  "campaign", "status", "disqualify_reason", "owner", "business_partner", "opportunity",
                  *AUDIT_FIELDS]
        read_only_fields = fields

    PERSONAL = ("contact_name", "email", "phone")

    def to_representation(self, instance):
        """Personal data is need-to-know (POPIA, ADR 0012): only the sales team and administrators see a
        lead's contact details; finance and viewers get the lead without them."""
        data = super().to_representation(instance)
        user = getattr(self.context.get("request"), "user", None)
        if not (user and (user.is_superuser or user.role in (Role.SALES_REP, Role.ADMIN))):
            for name in self.PERSONAL:
                data[name] = None
        return data


class LeadCreateSerializer(serializers.Serializer):
    company_code = _CompanyField()
    company_name = serializers.CharField(max_length=200)
    contact_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    source = serializers.ChoiceField(choices=Lead.Source.choices, default=Lead.Source.OTHER)
    campaign = serializers.CharField(max_length=12, required=False, allow_blank=True)

    def post(self, user) -> Lead:
        data = dict(self.validated_data)
        campaign = None
        if data.pop("campaign", ""):
            campaign = Campaign.objects.filter(company_code=data["company_code"],
                                               code=self.validated_data["campaign"]).first()
            if campaign is None:
                raise serializers.ValidationError({"campaign": "No such campaign in this company code."})
        return run(services.create_lead, campaign=campaign, user=user, **data)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)


class ConvertSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    expected_close_date = serializers.DateField()
    expected_value = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0, default=0)
    customer = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all(),
                                            required=False, allow_null=True,
                                            help_text="An existing partner; leave out to create one from the lead.")


# --- Opportunities -----------------------------------------------------------------------------

class OpportunitySerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code", read_only=True)
    customer = serializers.CharField(source="customer.number", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    campaign = serializers.CharField(source="campaign.code", read_only=True, default=None)
    owner = serializers.CharField(source="owner.username", read_only=True, default=None)
    weighted_value = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    quotations = serializers.SlugRelatedField(slug_field="quotation_number", many=True, read_only=True)

    class Meta:
        model = Opportunity
        fields = ["id", "opportunity_number", "company_code", "customer", "customer_name", "title", "stage",
                  "probability", "expected_value", "weighted_value", "expected_close_date", "campaign", "owner",
                  "closed_on", "lost_reason", "quotations", *AUDIT_FIELDS]
        read_only_fields = fields


class OpportunityCreateSerializer(serializers.Serializer):
    company_code = _CompanyField()
    customer = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    title = serializers.CharField(max_length=200)
    expected_close_date = serializers.DateField()
    expected_value = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0, default=0)
    stage = serializers.ChoiceField(choices=[(s, s) for s in Opportunity.OPEN_STAGES], default="PROSPECTING")
    campaign = serializers.CharField(max_length=12, required=False, allow_blank=True)

    def post(self, user) -> Opportunity:
        data = dict(self.validated_data)
        code = data.pop("campaign", "")
        campaign = Campaign.objects.filter(company_code=data["company_code"], code=code).first() if code else None
        if code and campaign is None:
            raise serializers.ValidationError({"campaign": "No such campaign in this company code."})
        return run(services.create_opportunity, campaign=campaign, user=user, **data)


class MoveSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=[(s, s) for s in Opportunity.OPEN_STAGES])
    probability = serializers.IntegerField(min_value=0, max_value=100, required=False)


class LoseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)
    on = serializers.DateField()


# --- Quotations ------------------------------------------------------------------------------------

class QuotationItemInputSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3, min_value=Decimal("0.001"))
    net_price = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0, required=False,
                                         allow_null=True)


class QuotationCreateSerializer(serializers.Serializer):
    sales_organization = serializers.SlugRelatedField(slug_field="code",
                                                      queryset=SalesOrganization.objects.select_related("company_code"))
    customer = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    opportunity = serializers.CharField(max_length=12, required=False, allow_blank=True)
    quotation_date = serializers.DateField()
    valid_until = serializers.DateField()
    items = QuotationItemInputSerializer(many=True)

    def post(self, user) -> Quotation:
        data = self.validated_data
        opportunity = None
        if data.get("opportunity"):
            opportunity = Opportunity.objects.filter(company_code=data["sales_organization"].company_code,
                                                     opportunity_number=data["opportunity"].upper()).first()
            if opportunity is None:
                raise serializers.ValidationError({"opportunity": "No such opportunity in this company code."})
        items = [services.QuotationItemInput(material=i["material"], quantity=i["quantity"],
                                             net_price=i.get("net_price")) for i in data["items"]]
        return run(services.create_quotation, sales_organization=data["sales_organization"],
                   customer=data["customer"], quotation_date=data["quotation_date"], valid_until=data["valid_until"],
                   items=items, opportunity=opportunity, user=user)


class QuotationItemSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    description = serializers.CharField(source="material.description")
    net_amount = serializers.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        model = QuotationItem
        fields = ["item_number", "material", "description", "quantity", "net_price", "price_overridden", "net_amount"]
        read_only_fields = fields


class QuotationSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    sales_organization = serializers.CharField(source="sales_organization.code")
    customer = serializers.CharField(source="customer.number")
    customer_name = serializers.CharField(source="customer.name")
    opportunity = serializers.CharField(source="opportunity.opportunity_number", default=None)
    sales_order = serializers.CharField(source="sales_order.order_number", default=None)
    net_total = serializers.DecimalField(max_digits=18, decimal_places=2)
    items = QuotationItemSerializer(many=True)

    class Meta:
        model = Quotation
        fields = ["id", "quotation_number", "company_code", "sales_organization", "customer", "customer_name",
                  "opportunity", "quotation_date", "valid_until", "status", "net_total", "sales_order", "items",
                  *AUDIT_FIELDS]
        read_only_fields = fields


class AcceptSerializer(serializers.Serializer):
    on = serializers.DateField(help_text="The date the customer accepted: the sales order's date.")


# --- Service tickets -------------------------------------------------------------------------------

class ServiceTicketSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    customer = serializers.CharField(source="customer.number")
    customer_name = serializers.CharField(source="customer.name")
    sales_order = serializers.CharField(source="sales_order.order_number", default=None)
    material = serializers.CharField(source="material.number", default=None)
    owner = serializers.CharField(source="owner.username", default=None)

    class Meta:
        model = ServiceTicket
        fields = ["id", "ticket_number", "company_code", "customer", "customer_name", "subject", "description",
                  "priority", "status", "opened_at", "due_at", "resolved_at", "resolution", "sales_order",
                  "material", "owner", *AUDIT_FIELDS]
        read_only_fields = fields


class TicketCreateSerializer(serializers.Serializer):
    company_code = _CompanyField()
    customer = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    subject = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    priority = serializers.ChoiceField(choices=ServiceTicket.Priority.choices, default="MEDIUM")
    sales_order = serializers.CharField(max_length=12, required=False, allow_blank=True)
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all(), required=False,
                                            allow_null=True)

    def post(self, user) -> ServiceTicket:
        data = dict(self.validated_data)
        number = data.pop("sales_order", "")
        order = SalesOrder.objects.filter(company_code=data["company_code"], order_number=number.upper()).first() \
            if number else None
        if number and order is None:
            raise serializers.ValidationError({"sales_order": "No such sales order in this company code."})
        return run(services.open_ticket, sales_order=order, user=user, **data)


class ResolveSerializer(serializers.Serializer):
    resolution = serializers.CharField()


# --- Activities ------------------------------------------------------------------------------------

class ActivitySerializer(serializers.ModelSerializer):
    lead = serializers.SlugRelatedField(slug_field="lead_number", read_only=True)
    opportunity = serializers.SlugRelatedField(slug_field="opportunity_number", read_only=True)
    ticket = serializers.SlugRelatedField(slug_field="ticket_number", read_only=True)
    owner = serializers.CharField(source="owner.username", read_only=True, default=None)

    class Meta:
        model = Activity
        fields = ["id", "kind", "subject", "due_date", "completed_on", "owner", "lead", "opportunity", "ticket",
                  *AUDIT_FIELDS]
        read_only_fields = fields


class ActivityCreateSerializer(serializers.Serializer):
    company_code = _CompanyField()
    kind = serializers.ChoiceField(choices=Activity.Kind.choices)
    subject = serializers.CharField(max_length=200)
    due_date = serializers.DateField()
    lead = serializers.CharField(max_length=12, required=False, allow_blank=True)
    opportunity = serializers.CharField(max_length=12, required=False, allow_blank=True)
    ticket = serializers.CharField(max_length=12, required=False, allow_blank=True)

    def post(self, user) -> Activity:
        data = self.validated_data
        company = data["company_code"]
        targets = {
            "lead": Lead.objects.filter(company_code=company, lead_number=data.get("lead", "").upper()).first()
            if data.get("lead") else None,
            "opportunity": Opportunity.objects.filter(company_code=company,
                                                      opportunity_number=data.get("opportunity", "").upper()).first()
            if data.get("opportunity") else None,
            "ticket": ServiceTicket.objects.filter(company_code=company,
                                                   ticket_number=data.get("ticket", "").upper()).first()
            if data.get("ticket") else None,
        }
        missing = {name: "Not found in this company code." for name, obj in targets.items()
                   if data.get(name) and obj is None}
        if missing:
            raise serializers.ValidationError(missing)
        return run(services.log_activity, kind=data["kind"], subject=data["subject"], due_date=data["due_date"],
                   user=user, **targets)


class CompleteSerializer(serializers.Serializer):
    on = serializers.DateField()
