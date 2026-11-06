from rest_framework import viewsets

from apps.accounts.permissions import BusinessPartnerPermission, MaterialPermission, ReferenceDataPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin

from . import serializers
from .models import BusinessPartner, Currency, CustomerRole, ExchangeRate, Material, SupplierRole, UnitOfMeasure


class _MasterDataViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    pass


class CurrencyViewSet(_MasterDataViewSet):
    queryset = Currency.objects.all()
    serializer_class = serializers.CurrencySerializer
    permission_classes = [ReferenceDataPermission]
    lookup_field = "code"


class ExchangeRateViewSet(_MasterDataViewSet):
    queryset = ExchangeRate.objects.select_related("from_currency", "to_currency")
    serializer_class = serializers.ExchangeRateSerializer
    permission_classes = [ReferenceDataPermission]


class UnitOfMeasureViewSet(_MasterDataViewSet):
    queryset = UnitOfMeasure.objects.all()
    serializer_class = serializers.UnitOfMeasureSerializer
    permission_classes = [ReferenceDataPermission]
    lookup_field = "code"


class BusinessPartnerViewSet(_MasterDataViewSet):
    serializer_class = serializers.BusinessPartnerSerializer
    permission_classes = [BusinessPartnerPermission]
    lookup_field = "number"

    def get_queryset(self):
        queryset = BusinessPartner.objects.prefetch_related(
            "customer_roles__company_code", "customer_roles__reconciliation_account",
            "supplier_roles__company_code", "supplier_roles__reconciliation_account",
        )
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset


class CustomerRoleViewSet(_MasterDataViewSet):
    queryset = CustomerRole.objects.select_related("business_partner", "company_code", "reconciliation_account")
    serializer_class = serializers.CustomerRoleSerializer
    permission_classes = [BusinessPartnerPermission]


class SupplierRoleViewSet(_MasterDataViewSet):
    queryset = SupplierRole.objects.select_related("business_partner", "company_code", "reconciliation_account")
    serializer_class = serializers.SupplierRoleSerializer
    permission_classes = [BusinessPartnerPermission]


class MaterialViewSet(_MasterDataViewSet):
    queryset = Material.objects.select_related("base_unit")
    serializer_class = serializers.MaterialSerializer
    permission_classes = [MaterialPermission]
    lookup_field = "number"
