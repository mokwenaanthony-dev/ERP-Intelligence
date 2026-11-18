"""REST API views for planning (forecasts 8a, MRP 8b, risk and forecast accuracy 8c), under ``/api/planning/``.

    GET  demand-history/?plant=&first_period=&months=[&material=]   what customers took, per month
    GET  forecast-runs/[?plant=]  ·  GET forecast-runs/<id>/         stored runs
    POST forecast-runs/                                             forecast (201 new, 200 same history)
    GET  forecast/?plant=&material=                                 a material's latest forecast

Access (``PlanningPermission``): production planners and the warehouse run forecasts; finance,
purchasing, sales and viewers read them. Forecasting depends on ``ml_service``: if it is off or
unreachable the API answers 503, if it answers outside the contract 502 (ADR 0014).
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import InternalUser, PlanningPermission
from apps.masterdata.models import Material
from apps.organization.models import CompanyCode, Plant

from . import ml_client, mrp, risk, services
from .models import ForecastRun, MaterialPlanning, MRPRun, PlannedOrder
from .serializers import (ConvertSerializer, ForecastRequestSerializer, ForecastRunListSerializer,
                          ForecastRunSerializer, LatestForecastSerializer, MaterialPlanningSerializer,
                          MonthField, MRPRequestSerializer, MRPRunListSerializer, MRPRunSerializer,
                          PlannedOrderSerializer, run)


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The forecasting service is not available."


class BadGateway(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The forecasting service gave an answer the ERP cannot use."


class ForecastRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [PlanningPermission]

    def get_queryset(self):
        runs = ForecastRun.objects.select_related("company_code", "plant").prefetch_related(
            "items__material", "items__periods")
        plant = self.request.query_params.get("plant")
        return runs.filter(plant__code=plant) if plant else runs

    def get_serializer_class(self):
        return ForecastRunListSerializer if self.action == "list" else ForecastRunSerializer

    def create(self, request):
        data = ForecastRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            forecast_run, created = data.post(request.user)
        except ml_client.ServiceOff as exc:
            raise ServiceUnavailable(str(exc)) from exc
        except (ml_client.ServiceRejected, services.BadAnswer) as exc:
            raise BadGateway(str(exc)) from exc
        return Response(ForecastRunSerializer(self.get_queryset().get(pk=forecast_run.pk)).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class _PlantQuery(serializers.Serializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.all())
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all(), required=False)


class DemandHistoryQuery(_PlantQuery):
    first_period = MonthField(help_text="The month after the history, YYYY-MM.")
    months = serializers.IntegerField(min_value=1, max_value=services.HISTORY_RANGE[1], default=24)


class DemandHistoryView(APIView):
    permission_classes = [PlanningPermission]

    def get(self, request):
        query = DemandHistoryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        materials = [data["material"]] if data.get("material") else None
        histories = services.demand_history(data["plant"], data["first_period"], data["months"], materials)
        return Response({"plant": data["plant"].code, "unit": "base unit of each material", "materials": [
            {"material": h.material.number, "total": h.total,
             "history": [{"period": f"{services.add_months(h.start, i):%Y-%m}", "quantity": q}
                         for i, q in enumerate(h.quantities)]} for h in histories]})


class LatestForecastView(APIView):
    permission_classes = [PlanningPermission]

    def get(self, request):
        query = _PlantQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        if "material" not in query.validated_data:
            raise serializers.ValidationError({"material": ["This field is required."]})
        item = services.latest_forecast(query.validated_data["plant"], query.validated_data["material"])
        if item is None:
            return Response({"detail": "This material has not been forecast at this plant."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(LatestForecastSerializer(item).data)


# --- MRP (Wave 8b) -------------------------------------------------------------------------------------
#
#   CRUD material-planning/                    how each material is planned (planners)
#   GET  mrp-runs/[?plant=] · mrp-runs/<id>/   runs with requirements lists, planned orders, messages
#   POST mrp-runs/                             plan (201 new run, 200 the stored run for unchanged data)
#   GET  planned-orders/[?plant=&status=&procurement=]
#   POST planned-orders/<id>/convert/          MAKE: production planner; BUY: purchasing officer

class MaterialPlanningViewSet(viewsets.ModelViewSet):
    permission_classes = [PlanningPermission]
    serializer_class = MaterialPlanningSerializer

    def get_queryset(self):
        rows = MaterialPlanning.objects.select_related("material", "plant", "preferred_supplier")
        plant = self.request.query_params.get("plant")
        return rows.filter(plant__code=plant) if plant else rows


class MRPRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [PlanningPermission]

    def get_queryset(self):
        runs = MRPRun.objects.select_related("company_code", "plant").prefetch_related(
            "materials__material", "materials__lines", "planned_orders__material", "planned_orders__run__plant",
            "messages__material")
        plant = self.request.query_params.get("plant")
        return runs.filter(plant__code=plant) if plant else runs

    def get_serializer_class(self):
        return MRPRunListSerializer if self.action == "list" else MRPRunSerializer

    def create(self, request):
        data = MRPRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        mrp_run, created = data.post(request.user)
        return Response(MRPRunSerializer(self.get_queryset().get(pk=mrp_run.pk)).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


# Converting creates real orders, so it follows the role that owns them, not the planning roles.
CONVERT_ROLES = {MaterialPlanning.Procurement.MAKE: (Role.PRODUCTION_PLANNER,),
                 MaterialPlanning.Procurement.BUY: (Role.PURCHASING_OFFICER,)}


class PlannedOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PlannedOrderSerializer

    def get_permissions(self):
        # Reading follows the planning roles; converting is checked per order below.
        return [InternalUser()] if self.action == "convert" else [PlanningPermission()]

    def get_queryset(self):
        orders = PlannedOrder.objects.select_related("run__plant", "material")
        params = self.request.query_params
        for field, value in (("run__plant__code", params.get("plant")), ("status", params.get("status")),
                             ("procurement", params.get("procurement"))):
            if value:
                orders = orders.filter(**{field: value})
        return orders.order_by("-run__planning_date", "-run__id", "number")

    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        planned = self.get_object()
        user = request.user
        if not (user.is_superuser or user.role == Role.ADMIN or user.role in CONVERT_ROLES[planned.procurement]):
            raise PermissionDenied(f"Converting a {planned.procurement} planned order needs the "
                                   f"{' or '.join(r.label for r in CONVERT_ROLES[planned.procurement])} role.")
        data = ConvertSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        number = run(mrp.convert, planned, on=data.validated_data.get("on") or timezone.localdate(),
                     supplier=data.validated_data.get("supplier"), net_price=data.validated_data.get("net_price"),
                     user=user)
        planned.refresh_from_db()
        return Response({**PlannedOrderSerializer(planned).data, "document_number": number})


# --- Supply-chain risk and forecast accuracy (Wave 8c) -----------------------------------------------
#
#   GET supplier-risk/?company_code=1000[&months=3][&on=YYYY-MM-DD]   revenue at risk per supplier
#   GET forecast-accuracy/?plant=1000[&months=6][&on=YYYY-MM-DD]      forecasts against deliveries

class SupplierRiskQuery(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    months = serializers.IntegerField(min_value=1, max_value=12, default=3)
    on = serializers.DateField(required=False)


class SupplierRiskView(APIView):
    permission_classes = [PlanningPermission]

    def get(self, request):
        query = SupplierRiskQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        on = data.get("on") or timezone.localdate()
        rows = risk.supplier_risk(data["company_code"], on=on, months=data["months"])
        return Response({"company_code": data["company_code"].code, "on": on, "months": data["months"],
                         "revenue_at_risk": sum((r["revenue_at_risk"] for r in rows), Decimal("0")),
                         "suppliers": rows})


class ForecastAccuracyQuery(serializers.Serializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.all())
    months = serializers.IntegerField(min_value=1, max_value=24, default=6)
    on = serializers.DateField(required=False)


class ForecastAccuracyView(APIView):
    permission_classes = [PlanningPermission]

    def get(self, request):
        query = ForecastAccuracyQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        on = data.get("on") or timezone.localdate()
        return Response({"plant": data["plant"].code, "on": on, "months": data["months"],
                         "materials": risk.forecast_accuracy(data["plant"], on=on, months=data["months"])})
