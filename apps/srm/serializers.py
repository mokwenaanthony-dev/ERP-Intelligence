"""Serializers for SRM: validate API input and shape API output.

Input serializers check types and required fields; every business rule runs in ``services.py`` (called
from ``post()``, so a broken rule keeps its cause and the log shows its file and line) and comes back
as a 400 with the rule's message.
"""
from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import Role
from apps.finance.services import PostingError
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import CompanyCode, PurchasingOrganization

from . import services
from .models import Contract, RequestForQuotation, SupplierApplication, SupplierEvaluation, SupplierQuote

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]
QUANTITY = {"max_digits": 18, "decimal_places": 3, "min_value": Decimal("0.001")}
THOUSANDTH = Decimal("0.001")
MONEY = {"max_digits": 18, "decimal_places": 2, "min_value": Decimal("0")}


def run(func, *args, **kwargs):
    """Call a service; a broken rule becomes a 400 (the cause is kept, so the log shows file and line)."""
    try:
        return func(*args, **kwargs)
    except PostingError as exc:
        raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


def _company_field(**kwargs):
    return serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all(), **kwargs)


def _partner_field(**kwargs):
    return serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all(), **kwargs)


def _material_field(**kwargs):
    return serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all(), **kwargs)


def _purchasing_org_field():
    return serializers.SlugRelatedField(slug_field="code",
                                        queryset=PurchasingOrganization.objects.select_related("company_code"))


# --- Onboarding ----------------------------------------------------------------------------------

class ApplicationSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code", read_only=True)
    business_partner = serializers.CharField(source="business_partner.number", read_only=True, default=None)

    class Meta:
        model = SupplierApplication
        fields = ["id", "application_number", "company_code", "company_name", "registration_number", "vat_number",
                  "bbbee_level", "supplies", "contact_name", "email", "phone", "status", "rejection_reason",
                  "business_partner", *AUDIT_FIELDS]
        read_only_fields = fields

    PERSONAL = ("contact_name", "email", "phone")

    def to_representation(self, instance):
        """The applicant's contact details are need-to-know (POPIA, ADR 0013): purchasing and
        administrators see them; other roles get the application without them."""
        data = super().to_representation(instance)
        user = getattr(self.context.get("request"), "user", None)
        if not (user and (user.is_superuser or user.role in (Role.PURCHASING_OFFICER, Role.ADMIN))):
            for name in self.PERSONAL:
                data[name] = None
        return data


class ApplicationCreateSerializer(serializers.Serializer):
    company_code = _company_field()
    company_name = serializers.CharField(max_length=200)
    registration_number = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    vat_number = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    bbbee_level = serializers.IntegerField(min_value=1, max_value=8, required=False, allow_null=True, default=None)
    supplies = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    contact_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")

    def post(self, user):
        return run(services.submit_application, user=user, **self.validated_data)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=200)


# --- Requests for quotation ------------------------------------------------------------------------

class RFQItemInputSerializer(serializers.Serializer):
    material = _material_field()
    quantity = serializers.DecimalField(**QUANTITY)


class RFQCreateSerializer(serializers.Serializer):
    purchasing_organization = _purchasing_org_field()
    title = serializers.CharField(max_length=200)
    deadline = serializers.DateField()
    items = RFQItemInputSerializer(many=True)
    suppliers = _partner_field(many=True)

    def post(self, user):
        data = self.validated_data
        items = [services.RFQItemInput(material=i["material"], quantity=i["quantity"]) for i in data["items"]]
        return run(services.create_rfq, purchasing_organization=data["purchasing_organization"], title=data["title"],
                   deadline=data["deadline"], items=items, suppliers=data["suppliers"], user=user)


class QuoteSerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(source="supplier.number")
    supplier_name = serializers.CharField(source="supplier.name")
    total = serializers.SerializerMethodField()
    prices = serializers.SerializerMethodField()

    class Meta:
        model = SupplierQuote
        fields = ["id", "supplier", "supplier_name", "status", "delivery_days", "submitted_on", "supplier_reference",
                  "total", "prices"]
        read_only_fields = fields

    def get_total(self, quote):
        return services.quote_total(quote) if quote.status != SupplierQuote.Status.INVITED else None

    def get_prices(self, quote):
        return {qi.rfq_item.material.number: qi.net_price for qi in quote.items.select_related("rfq_item__material")}


class RFQSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    purchasing_organization = serializers.CharField(source="purchasing_organization.code")
    purchase_order = serializers.CharField(source="purchase_order.order_number", default=None)
    items = serializers.SerializerMethodField()
    quotes = QuoteSerializer(many=True)

    class Meta:
        model = RequestForQuotation
        fields = ["id", "rfq_number", "company_code", "purchasing_organization", "title", "deadline", "status",
                  "purchase_order", "items", "quotes", *AUDIT_FIELDS]
        read_only_fields = fields

    def get_items(self, rfq):
        return [{"item_number": i.item_number, "material": i.material.number, "description": i.material.description,
                 "quantity": i.quantity} for i in rfq.items.select_related("material")]


class QuoteSubmitSerializer(serializers.Serializer):
    supplier = _partner_field(help_text="The invited supplier whose quote this is.")
    prices = serializers.DictField(child=serializers.DecimalField(**MONEY),
                                   help_text='{"TG-1001": "185.00", ...}: a price for every material asked for.')
    delivery_days = serializers.IntegerField(min_value=0)
    submitted_on = serializers.DateField()
    supplier_reference = serializers.CharField(max_length=35, required=False, allow_blank=True, default="")

    def validate_prices(self, prices):
        materials = {m.number: m for m in Material.objects.filter(number__in=list(prices))}
        unknown = sorted(set(prices) - set(materials))
        if unknown:
            raise serializers.ValidationError(f"Unknown material(s): {', '.join(unknown)}.")
        return {materials[number]: price for number, price in prices.items()}


class AwardSerializer(serializers.Serializer):
    supplier = _partner_field()
    on = serializers.DateField(help_text="The order date of the purchase order.")


# --- Contracts ---------------------------------------------------------------------------------

class ContractItemInputSerializer(serializers.Serializer):
    material = _material_field()
    net_price = serializers.DecimalField(**MONEY)
    target_quantity = serializers.DecimalField(**QUANTITY)


class ContractCreateSerializer(serializers.Serializer):
    purchasing_organization = _purchasing_org_field()
    supplier = _partner_field()
    valid_from = serializers.DateField()
    valid_to = serializers.DateField()
    items = ContractItemInputSerializer(many=True)

    def post(self, user):
        data = self.validated_data
        items = [services.ContractItemInput(**item) for item in data["items"]]
        return run(services.create_contract, purchasing_organization=data["purchasing_organization"],
                   supplier=data["supplier"], valid_from=data["valid_from"], valid_to=data["valid_to"], items=items,
                   user=user)


class ContractSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    purchasing_organization = serializers.CharField(source="purchasing_organization.code")
    supplier = serializers.CharField(source="supplier.number")
    supplier_name = serializers.CharField(source="supplier.name")
    items = serializers.SerializerMethodField()
    releases = serializers.SerializerMethodField()

    class Meta:
        model = Contract
        fields = ["id", "contract_number", "company_code", "purchasing_organization", "supplier", "supplier_name",
                  "valid_from", "valid_to", "status", "items", "releases", *AUDIT_FIELDS]
        read_only_fields = fields

    def get_items(self, contract):
        # A database SUM may lose the scale ("10" not "10.000"), so quantities are put back to 3 places.
        released = {material: Decimal(total).quantize(THOUSANDTH)
                    for material, total in services.released_quantities(contract).items()}
        rows = []
        for i in contract.items.select_related("material"):
            done = released.get(i.material_id, Decimal("0.000"))
            rows.append({"item_number": i.item_number, "material": i.material.number, "net_price": i.net_price,
                         "target_quantity": i.target_quantity, "released_quantity": done,
                         "open_quantity": (i.target_quantity - done).quantize(THOUSANDTH)})
        return rows

    def get_releases(self, contract):
        return [r.purchase_order.order_number for r in contract.releases.select_related("purchase_order")]


class ReleaseItemSerializer(serializers.Serializer):
    material = _material_field()
    quantity = serializers.DecimalField(**QUANTITY)


class ReleaseSerializer(serializers.Serializer):
    order_date = serializers.DateField()
    delivery_date = serializers.DateField(required=False, allow_null=True, default=None)
    items = ReleaseItemSerializer(many=True)


# --- Evaluation ----------------------------------------------------------------------------------

class EvaluationSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    supplier = serializers.CharField(source="supplier.number")
    supplier_name = serializers.CharField(source="supplier.name")
    evaluated_by = serializers.CharField(source="evaluated_by.username", default=None)

    class Meta:
        model = SupplierEvaluation
        fields = ["id", "company_code", "supplier", "supplier_name", "period_start", "period_end", "on_time_rate",
                  "in_full_rate", "price_rate", "quality_score", "overall_score", "grade", "evaluated_on",
                  "evaluated_by"]
        read_only_fields = fields


class EvaluateSerializer(serializers.Serializer):
    company_code = _company_field()
    supplier = _partner_field()
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    quality_score = serializers.IntegerField(min_value=1, max_value=5)
    evaluated_on = serializers.DateField()

    def post(self, user):
        return run(services.evaluate_supplier, user=user, **self.validated_data)
