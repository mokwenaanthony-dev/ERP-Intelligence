from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import (
    AnalyticsAdminPermission,
    FinancialReportPermission,
    MarginReportPermission,
    PayrollReportPermission,
    StockReportPermission,
)
from apps.controlling.models import ProfitCenter
from apps.finance.views import _company_code, _int_param

from . import services


def _year(request, name="fiscal_year"):
    year = _int_param(request, name)
    if year is None:
        raise serializers.ValidationError({name: "Required."})
    return year


def _period_range(request):
    period_from, period_to = _int_param(request, "period_from", 1), _int_param(request, "period_to", 12)
    if not 1 <= period_from <= period_to <= 12:
        raise serializers.ValidationError({"period_from": "Periods must satisfy 1 <= from <= to <= 12."})
    return period_from, period_to


class IncomeStatementView(APIView):
    """GET /api/analytics/income-statement/?company_code=1000&fiscal_year=2027
    [&period_from=&period_to=][&profit_center=PC1000]"""

    permission_classes = [FinancialReportPermission]

    def get(self, request):
        company_code = _company_code(request)
        profit_center = None
        if request.query_params.get("profit_center"):
            profit_center = ProfitCenter.objects.filter(company_code=company_code,
                                                        code=request.query_params["profit_center"]).first()
            if profit_center is None:
                raise serializers.ValidationError({"profit_center": "Profit centre not found."})
        return Response(services.income_statement(company_code, _year(request), *_period_range(request),
                                                  profit_center=profit_center))


class BalanceSheetView(APIView):
    """GET /api/analytics/balance-sheet/?company_code=1000&fiscal_year=2027&period=6"""

    permission_classes = [FinancialReportPermission]

    def get(self, request):
        period = _int_param(request, "period", 12)
        if not 1 <= period <= 12:
            raise serializers.ValidationError({"period": "Must be between 1 and 12."})
        return Response(services.balance_sheet(_company_code(request), _year(request), period))


class SalesMarginView(APIView):
    """GET /api/analytics/sales-margin/?company_code=1000&fiscal_year=2027
    [&group_by=customer|material|profit_center][&period_from=&period_to=]"""

    permission_classes = [MarginReportPermission]

    def get(self, request):
        group_by = request.query_params.get("group_by", "customer")
        if group_by not in services.MARGIN_GROUPS:
            raise serializers.ValidationError({"group_by": f"Use one of {', '.join(services.MARGIN_GROUPS)}."})
        return Response(services.sales_margin(_company_code(request), _year(request), *_period_range(request),
                                              group_by=group_by))


class StockAgeingView(APIView):
    """GET /api/analytics/stock-ageing/?company_code=1000[&as_of=2026-11-30][&slow_after_days=90]"""

    permission_classes = [StockReportPermission]

    def get(self, request):
        as_of = None
        if request.query_params.get("as_of"):
            as_of = serializers.DateField().to_internal_value(request.query_params["as_of"])
        slow = _int_param(request, "slow_after_days", 90)
        return Response(services.stock_ageing(_company_code(request), as_of, slow))


class PayrollCostView(APIView):
    """GET /api/analytics/payroll-cost/?company_code=1000[&year=2026][&by_cost_center=true]"""

    permission_classes = [PayrollReportPermission]

    def get(self, request):
        by_cost_center = request.query_params.get("by_cost_center") in ("1", "true", "yes")
        return Response(services.payroll_cost_trend(_company_code(request), _int_param(request, "year"),
                                                    by_cost_center))


class RefreshView(APIView):
    """GET /api/analytics/refresh/ - when each view was last refreshed.
    POST - refresh all materialized views now (finance manager)."""

    permission_classes = [AnalyticsAdminPermission]

    def get(self, request):
        return Response(services.freshness())

    def post(self, request):
        return Response({"refreshed": services.refresh_views(), **services.freshness()}, status=status.HTTP_200_OK)
