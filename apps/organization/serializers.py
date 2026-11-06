from rest_framework import serializers

from apps.finance.models import ChartOfAccounts
from apps.masterdata.models import Currency

from .models import CompanyCode, Plant, PurchasingOrganization, SalesOrganization, StorageLocation

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


class CompanyCodeSerializer(serializers.ModelSerializer):
    currency = serializers.SlugRelatedField(slug_field="code", queryset=Currency.objects.all())
    chart_of_accounts = serializers.SlugRelatedField(slug_field="code", queryset=ChartOfAccounts.objects.all())

    class Meta:
        model = CompanyCode
        fields = ["id", "code", "name", "country", "currency", "chart_of_accounts", "vat_registration_number",
                  "fiscal_year_start_month", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class _CompanyCodeChildSerializer(serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())


class PlantSerializer(_CompanyCodeChildSerializer):
    class Meta:
        model = Plant
        fields = ["id", "code", "name", "company_code", "address", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class StorageLocationSerializer(serializers.ModelSerializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.all())

    class Meta:
        model = StorageLocation
        fields = ["id", "plant", "code", "name", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class SalesOrganizationSerializer(_CompanyCodeChildSerializer):
    class Meta:
        model = SalesOrganization
        fields = ["id", "code", "name", "company_code", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class PurchasingOrganizationSerializer(_CompanyCodeChildSerializer):
    class Meta:
        model = PurchasingOrganization
        fields = ["id", "code", "name", "company_code", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]
