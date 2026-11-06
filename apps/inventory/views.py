from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import FinanceConfigPermission, InventoryPermission
from apps.common.views import AuditedViewMixin
from apps.finance.serializers import ReverseSerializer
from apps.masterdata.models import Material
from apps.organization.models import CompanyCode, Plant

from . import services
from .models import InventoryAccountSetting, MaterialDocument
from .serializers import (
    GoodsMovementCreateSerializer,
    InventoryAccountSettingSerializer,
    MaterialDocumentSerializer,
)


# --- Stock ledger ---------------------------------------------------------------------------

class GoodsMovementViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Post (create), list, view and reverse goods movements (material documents)."""

    serializer_class = MaterialDocumentSerializer
    permission_classes = [InventoryPermission]

    def get_queryset(self):
        queryset = MaterialDocument.objects.select_related(
            "company_code", "plant", "business_partner", "journal_entry", "reversal_of", "reversed_by"
        ).prefetch_related("items__material", "items__storage_location", "items__unit")
        params = self.request.query_params
        if params.get("plant"):
            queryset = queryset.filter(plant__code=params["plant"])
        if params.get("movement_type"):
            queryset = queryset.filter(movement_type=params["movement_type"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("material"):
            queryset = queryset.filter(items__material__number=params["material"]).distinct()
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = GoodsMovementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = serializer.post(request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=document.pk)).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        params = ReverseSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            reversal = services.reverse_material_document(
                self.get_object(), posting_date=params.validated_data.get("posting_date"), user=request.user
            )
        except services.PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc
        return Response(self.get_serializer(self.get_queryset().get(pk=reversal.pk)).data,
                        status=status.HTTP_201_CREATED)


class StockOverviewView(APIView):
    """GET /api/inventory/stock/[?plant=1000][&material=TG-1001]"""

    permission_classes = [InventoryPermission]

    def get(self, request):
        plant = material = None
        if request.query_params.get("plant"):
            plant = Plant.objects.filter(code=request.query_params["plant"]).first()
            if plant is None:
                raise serializers.ValidationError({"plant": "Plant not found."})
        if request.query_params.get("material"):
            material = Material.objects.filter(number=request.query_params["material"]).first()
            if material is None:
                raise serializers.ValidationError({"material": "Material not found."})
        return Response(services.stock_overview(plant=plant, material=material))


class StockReconciliationView(APIView):
    """GET /api/inventory/reports/stock-reconciliation/?company_code=1000"""

    permission_classes = [InventoryPermission]

    def get(self, request):
        company_code = CompanyCode.objects.select_related("currency", "chart_of_accounts").filter(
            code=request.query_params.get("company_code", "")
        ).first()
        if company_code is None:
            raise serializers.ValidationError({"company_code": "A valid company_code is required."})
        return Response(services.stock_reconciliation(company_code))


class InventoryAccountSettingViewSet(AuditedViewMixin, viewsets.ModelViewSet):
    """Account determination for inventory postings. Maintained by the Finance Manager."""

    queryset = InventoryAccountSetting.objects.select_related("chart_of_accounts", "gl_account")
    serializer_class = InventoryAccountSettingSerializer
    permission_classes = [FinanceConfigPermission]
