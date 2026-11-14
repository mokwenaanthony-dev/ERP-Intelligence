"""REST API views for planning, under ``/api/planning/``.

    GET  demand-history/?plant=&first_period=&months=[&material=]   what customers took, per month
    GET  forecast-runs/[?plant=]  ·  GET forecast-runs/<id>/         stored runs
    POST forecast-runs/                                             forecast (201 new, 200 same history)
    GET  forecast/?plant=&material=                                 a material's latest forecast

Access (``PlanningPermission``): production planners and the warehouse run forecasts; finance,
purchasing, sales and viewers read them. Forecasting depends on ``ml_service``: if it is off or
unreachable the API answers 503, if it answers outside the contract 502 (ADR 0014).
"""
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import PlanningPermission
from apps.masterdata.models import Material
from apps.organization.models import Plant

from . import ml_client, services
from .models import ForecastRun
from .serializers import (ForecastRequestSerializer, ForecastRunListSerializer, ForecastRunSerializer,
                          LatestForecastSerializer, MonthField)


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
