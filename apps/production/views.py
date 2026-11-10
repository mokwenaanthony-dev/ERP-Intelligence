"""REST API views for production planning.

Views stay thin: they check the role, validate input with a serializer and call the service layer
(``services.py``), which holds every business rule. A rule broken in the service raises
``ProductionError``; the serializer turns it into a 400 and ``apps.common.exceptions`` logs it with
the file and line where it was raised.

BOMs and routings are created whole and never edited: a change is a new version with a later
``valid_from``. The ``status`` action activates or deactivates a version.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import ActivityRatePermission, CostEstimatePermission, ProductionPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin
from apps.masterdata.models import Material
from apps.organization.models import Plant

from . import services
from .models import ActivityRate, ActivityType, BillOfMaterials, ProductionArea, ProductionLine, Routing, WorkCenter
from .serializers import (
    ActivityRateSerializer,
    ActivityTypeSerializer,
    BOMCreateSerializer,
    BOMSerializer,
    ProductionAreaSerializer,
    ProductionLineSerializer,
    RoutingCreateSerializer,
    RoutingSerializer,
    StatusSerializer,
    WorkCenterSerializer,
    rule_error,
)


def _plant_filter(queryset, request, field="plant__code"):
    if request.query_params.get("plant"):
        queryset = queryset.filter(**{field: request.query_params["plant"]})
    return queryset


class _MasterDataViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    permission_classes = [ProductionPermission]


class ProductionAreaViewSet(_MasterDataViewSet):
    serializer_class = ProductionAreaSerializer

    def get_queryset(self):
        return _plant_filter(ProductionArea.objects.select_related("plant__company_code"), self.request)


class ProductionLineViewSet(_MasterDataViewSet):
    serializer_class = ProductionLineSerializer

    def get_queryset(self):
        return _plant_filter(ProductionLine.objects.select_related("area__plant__company_code"), self.request,
                             "area__plant__code")


class WorkCenterViewSet(_MasterDataViewSet):
    serializer_class = WorkCenterSerializer

    def get_queryset(self):
        queryset = WorkCenter.objects.select_related("plant", "line__area__plant__company_code", "cost_center")
        return _plant_filter(queryset.prefetch_related("activity_types"), self.request)


class ActivityTypeViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    serializer_class = ActivityTypeSerializer
    permission_classes = [ActivityRatePermission]

    def get_queryset(self):
        queryset = ActivityType.objects.select_related("company_code", "unit", "allocation_account")
        if self.request.query_params.get("company_code"):
            queryset = queryset.filter(company_code__code=self.request.query_params["company_code"])
        return queryset


class ActivityRateViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    serializer_class = ActivityRateSerializer
    permission_classes = [ActivityRatePermission]
    queryset = ActivityRate.objects.select_related("cost_center", "activity_type")


class _RecipeViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List, view and create versions; activate or deactivate them. No edits, no deletes."""

    permission_classes = [ProductionPermission]
    create_serializer = None
    set_status = None

    def filter(self, queryset):
        params = self.request.query_params
        queryset = _plant_filter(queryset, self.request)
        if params.get("material"):
            queryset = queryset.filter(material__number=params["material"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.create_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = serializer.post(request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=record.pk)).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="status")
    def change_status(self, request, pk=None):
        """POST {"status": "ACTIVE" | "INACTIVE"}"""
        serializer = StatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            record = type(self).set_status(self.get_object(), serializer.validated_data["status"], request.user)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc
        return Response(self.get_serializer(self.get_queryset().get(pk=record.pk)).data)


class BOMViewSet(_RecipeViewSet):
    serializer_class = BOMSerializer
    create_serializer = BOMCreateSerializer
    set_status = staticmethod(services.set_bom_status)

    def get_queryset(self):
        return self.filter(BillOfMaterials.objects.select_related("material", "plant", "created_by", "updated_by")
                           .prefetch_related("items__component__base_unit"))

    @action(detail=False, methods=["get"])
    def explode(self, request):
        """GET ?material=FG-4001&plant=1000&quantity=10[&date=2026-06-01]: all components, all levels."""
        material, plant, quantity, on = _estimate_params(request)
        try:
            rows = services.explode_bom(material, plant, quantity, on)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc
        return Response({"material": material.number, "plant": plant.code, "quantity": quantity, "date": on,
                         "components": rows})


class RoutingViewSet(_RecipeViewSet):
    serializer_class = RoutingSerializer
    create_serializer = RoutingCreateSerializer
    set_status = staticmethod(services.set_routing_status)

    def get_queryset(self):
        return self.filter(Routing.objects.select_related("material", "plant", "created_by", "updated_by")
                           .prefetch_related("operations__work_center", "operations__activities__activity_type"))


def _estimate_params(request):
    params = request.query_params
    material = Material.objects.filter(number=params.get("material", "")).first()
    plant = Plant.objects.select_related("company_code__currency").filter(code=params.get("plant", "")).first()
    errors = {}
    if material is None:
        errors["material"] = "A valid material number is required."
    if plant is None:
        errors["plant"] = "A valid plant code is required."
    try:
        quantity = Decimal(params.get("quantity", "1"))
        if quantity <= 0:
            raise InvalidOperation
    except InvalidOperation:
        errors["quantity"] = "Must be a number greater than zero."
    try:
        on = date.fromisoformat(params["date"]) if params.get("date") else date.today()
    except ValueError:
        errors["date"] = "Use YYYY-MM-DD."
    if errors:
        raise serializers.ValidationError(errors)
    return material, plant, quantity, on


class CostEstimateView(APIView):
    """GET /api/production/cost-estimate/?material=FG-4001&plant=1000&quantity=100[&date=2026-06-01]"""

    permission_classes = [CostEstimatePermission]

    def get(self, request):
        material, plant, quantity, on = _estimate_params(request)
        try:
            return Response(services.cost_estimate(material, plant, quantity, on))
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class EquipmentView(APIView):
    """GET /api/production/equipment/?plant=1000: the ISA-95 equipment tree with UNS topic paths."""

    permission_classes = [ProductionPermission]

    def get(self, request):
        plant = Plant.objects.select_related("company_code").filter(code=request.query_params.get("plant", "")).first()
        if plant is None:
            raise serializers.ValidationError({"plant": "A valid plant code is required."})
        return Response(services.equipment_hierarchy(plant))
