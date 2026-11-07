from rest_framework import serializers, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import ControllingPermission, FinancePermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin
from apps.finance.views import _company_code, _int_param

from . import services
from .models import CostCenter, PlantProfitCenter, ProfitCenter
from .serializers import CostCenterSerializer, PlantProfitCenterSerializer, ProfitCenterSerializer


class _ControllingViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    permission_classes = [ControllingPermission]

    def get_queryset(self):
        queryset = self.queryset
        if self.request.query_params.get("company_code"):
            queryset = queryset.filter(company_code__code=self.request.query_params["company_code"])
        return queryset


class ProfitCenterViewSet(_ControllingViewSet):
    queryset = ProfitCenter.objects.select_related("company_code")
    serializer_class = ProfitCenterSerializer


class CostCenterViewSet(_ControllingViewSet):
    queryset = CostCenter.objects.select_related("company_code", "profit_center")
    serializer_class = CostCenterSerializer


class PlantProfitCenterViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    queryset = PlantProfitCenter.objects.select_related("plant", "profit_center")
    serializer_class = PlantProfitCenterSerializer
    permission_classes = [ControllingPermission]


def _periods(request):
    year = _int_param(request, "fiscal_year")
    if year is None:
        raise serializers.ValidationError({"fiscal_year": "Required."})
    return year, _int_param(request, "period_from", 1), _int_param(request, "period_to", 12)


class CostCenterReportView(APIView):
    """GET /api/controlling/reports/cost-centers/?company_code=1000&fiscal_year=2027
    [&period_from=&period_to=][&cost_center=2000]"""

    permission_classes = [FinancePermission]

    def get(self, request):
        company_code = _company_code(request)
        year, period_from, period_to = _periods(request)
        cost_center = None
        if request.query_params.get("cost_center"):
            cost_center = CostCenter.objects.filter(company_code=company_code,
                                                    code=request.query_params["cost_center"]).first()
            if cost_center is None:
                raise serializers.ValidationError({"cost_center": "Cost centre not found."})
        return Response(services.cost_center_report(company_code, year, period_from, period_to, cost_center))


class ProfitCenterReportView(APIView):
    """GET /api/controlling/reports/profit-centers/?company_code=1000&fiscal_year=2027[&period_from=&period_to=]"""

    permission_classes = [FinancePermission]

    def get(self, request):
        company_code = _company_code(request)
        year, period_from, period_to = _periods(request)
        return Response(services.profit_center_report(company_code, year, period_from, period_to))
