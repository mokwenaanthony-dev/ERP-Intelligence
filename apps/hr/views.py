from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.permissions import HRPermission, PayrollPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin
from apps.finance.services import PostingError

from . import services
from .models import Employee, OrgUnit, PayrollRun, Position
from .serializers import (
    EmployeeSerializer,
    OrgUnitSerializer,
    PayrollRunCreateSerializer,
    PayrollRunSerializer,
    PositionSerializer,
    PostingDateSerializer,
)


class _HRViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    permission_classes = [HRPermission]


class OrgUnitViewSet(_HRViewSet):
    queryset = OrgUnit.objects.select_related("company_code", "cost_center", "parent")
    serializer_class = OrgUnitSerializer


class PositionViewSet(_HRViewSet):
    queryset = Position.objects.select_related("org_unit__company_code", "org_unit__cost_center", "cost_center")
    serializer_class = PositionSerializer


class EmployeeViewSet(_HRViewSet):
    """Employees can be changed (salary, position, termination) but not deleted once paid."""

    serializer_class = EmployeeSerializer

    def get_queryset(self):
        queryset = Employee.objects.select_related(
            "company_code", "position__cost_center", "position__org_unit__cost_center"
        )
        params = self.request.query_params
        if params.get("company_code"):
            queryset = queryset.filter(company_code__code=params["company_code"])
        if params.get("org_unit"):
            queryset = queryset.filter(position__org_unit__code=params["org_unit"])
        if params.get("active") in ("1", "true", "yes"):
            queryset = queryset.filter(termination_date__isnull=True)
        return queryset


class PayrollRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """POST {"company_code", "year", "month"[, "posting_date"]} calculates and posts payroll; reverse via action."""

    serializer_class = PayrollRunSerializer
    permission_classes = [PayrollPermission]

    def get_queryset(self):
        queryset = PayrollRun.objects.select_related("company_code", "journal_entry").prefetch_related(
            "results__employee", "results__cost_center"
        )
        params = self.request.query_params
        if params.get("year"):
            queryset = queryset.filter(year=params["year"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = PayrollRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = serializer.post(request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=run.pk)).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        params = PostingDateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            run = services.reverse_payroll_run(self.get_object(), user=request.user,
                                               posting_date=params.validated_data.get("posting_date"))
        except PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc
        return Response(self.get_serializer(self.get_queryset().get(pk=run.pk)).data)
