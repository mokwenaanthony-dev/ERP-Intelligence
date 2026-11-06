from rest_framework import serializers

from apps.finance.models import GLAccount
from apps.organization.models import CompanyCode

from .models import BusinessPartner, Currency, CustomerRole, ExchangeRate, Material, SupplierRole, UnitOfMeasure

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ["id", "code", "name", "decimal_places", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class ExchangeRateSerializer(serializers.ModelSerializer):
    from_currency = serializers.SlugRelatedField(slug_field="code", queryset=Currency.objects.all())
    to_currency = serializers.SlugRelatedField(slug_field="code", queryset=Currency.objects.all())

    class Meta:
        model = ExchangeRate
        fields = ["id", "from_currency", "to_currency", "rate", "valid_from", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        source = attrs.get("from_currency", getattr(self.instance, "from_currency", None))
        target = attrs.get("to_currency", getattr(self.instance, "to_currency", None))
        if source is not None and source == target:
            raise serializers.ValidationError("From and to currency must differ.")
        if attrs.get("rate") is not None and attrs["rate"] <= 0:
            raise serializers.ValidationError({"rate": "Rate must be greater than zero."})
        return attrs


class UnitOfMeasureSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitOfMeasure
        fields = ["id", "code", "name", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class _RoleSerializer(serializers.ModelSerializer):
    """Customer/supplier role. The reconciliation account is given by its number."""

    RECONCILIATION_TYPE = None  # set by subclasses

    business_partner = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    reconciliation_account = serializers.CharField(write_only=True)
    reconciliation_account_number = serializers.CharField(source="reconciliation_account.number", read_only=True)

    def validate(self, attrs):
        company_code = attrs.get("company_code", getattr(self.instance, "company_code", None))
        number = attrs.get("reconciliation_account")
        if isinstance(number, str):
            account = GLAccount.objects.filter(
                chart_of_accounts=company_code.chart_of_accounts, number=number
            ).first()
            if account is None:
                raise serializers.ValidationError({
                    "reconciliation_account": f"Account {number} is not in the chart of {company_code.code}."
                })
            if account.reconciliation_type != self.RECONCILIATION_TYPE:
                raise serializers.ValidationError(
                    {"reconciliation_account": f"Account {number} is not a {self.RECONCILIATION_TYPE.lower()} "
                                               f"reconciliation account."}
                )
            attrs["reconciliation_account"] = account
        return attrs


class CustomerRoleSerializer(_RoleSerializer):
    RECONCILIATION_TYPE = GLAccount.ReconciliationType.CUSTOMER

    class Meta:
        model = CustomerRole
        fields = ["id", "business_partner", "company_code", "reconciliation_account", "reconciliation_account_number",
                  "payment_terms_days", "credit_limit", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class SupplierRoleSerializer(_RoleSerializer):
    RECONCILIATION_TYPE = GLAccount.ReconciliationType.SUPPLIER

    class Meta:
        model = SupplierRole
        fields = ["id", "business_partner", "company_code", "reconciliation_account", "reconciliation_account_number",
                  "payment_terms_days", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class _RoleSummarySerializer(serializers.Serializer):
    company_code = serializers.CharField(source="company_code.code")
    reconciliation_account = serializers.CharField(source="reconciliation_account.number")
    payment_terms_days = serializers.IntegerField()


class BusinessPartnerSerializer(serializers.ModelSerializer):
    customer_roles = _RoleSummarySerializer(many=True, read_only=True)
    supplier_roles = _RoleSummarySerializer(many=True, read_only=True)

    class Meta:
        model = BusinessPartner
        fields = ["id", "number", "category", "name", "vat_number", "registration_number", "email", "phone",
                  "street", "city", "postal_code", "country", "is_blocked", "customer_roles", "supplier_roles",
                  *AUDIT_FIELDS]
        read_only_fields = ["id", "number", *AUDIT_FIELDS]


class MaterialSerializer(serializers.ModelSerializer):
    base_unit = serializers.SlugRelatedField(slug_field="code", queryset=UnitOfMeasure.objects.all())

    class Meta:
        model = Material
        fields = ["id", "number", "description", "material_type", "base_unit", "material_group", "is_active",
                  *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]
