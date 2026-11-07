"""REST API views for company codes, plants, storage locations, sales and purchasing organisations.

Views stay thin: they check the role, validate input with a serializer and call the service layer
(``services.py``), which holds every business rule. A rule broken in the service raises
``PostingError``; the serializer turns it into a 400 and ``apps.common.exceptions`` logs it with
the file and line where it was raised.
"""
from rest_framework import viewsets

from apps.accounts.permissions import OrganizationPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin

from . import serializers
from .models import CompanyCode, Plant, PurchasingOrganization, SalesOrganization, StorageLocation


class _OrgViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    permission_classes = [OrganizationPermission]
    lookup_field = "code"


class CompanyCodeViewSet(_OrgViewSet):
    queryset = CompanyCode.objects.select_related("currency", "chart_of_accounts")
    serializer_class = serializers.CompanyCodeSerializer


class PlantViewSet(_OrgViewSet):
    queryset = Plant.objects.select_related("company_code")
    serializer_class = serializers.PlantSerializer


class StorageLocationViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    queryset = StorageLocation.objects.select_related("plant")
    serializer_class = serializers.StorageLocationSerializer
    permission_classes = [OrganizationPermission]


class SalesOrganizationViewSet(_OrgViewSet):
    queryset = SalesOrganization.objects.select_related("company_code")
    serializer_class = serializers.SalesOrganizationSerializer


class PurchasingOrganizationViewSet(_OrgViewSet):
    queryset = PurchasingOrganization.objects.select_related("company_code")
    serializer_class = serializers.PurchasingOrganizationSerializer
