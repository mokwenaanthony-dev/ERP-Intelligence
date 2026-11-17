"""REST API of the control tower (Wave 9a), under ``/api/control/``. Read-only.

    GET kpis/?company_code=1000[&on=YYYY-MM-DD][&days=90][&months=3]   KPIs now and ahead, with status
    GET problems/?company_code=1000[&on=][&area=][&severity=]           the problems inbox, filtered by role

Every role except HR may read (``ControlPermission``); the inbox shows only the areas the user's
role may see in the rest of the API.
"""
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import ControlPermission
from apps.organization.models import CompanyCode

from . import services


class _Query(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.select_related(
        "currency"))
    on = serializers.DateField(required=False)


class KPIQuery(_Query):
    days = serializers.IntegerField(min_value=7, max_value=365, default=90)
    months = serializers.IntegerField(min_value=1, max_value=12, default=3)


class ProblemQuery(_Query):
    area = serializers.ChoiceField(choices=sorted(services.AREA_ROLES), required=False)
    severity = serializers.ChoiceField(choices=[services.CRITICAL, services.WARNING], required=False)


class KPIView(APIView):
    permission_classes = [ControlPermission]

    def get(self, request):
        query = KPIQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        return Response(services.kpis(data["company_code"], on=data.get("on") or timezone.localdate(),
                                      days=data["days"], months=data["months"]))


class ProblemView(APIView):
    permission_classes = [ControlPermission]

    def get(self, request):
        query = ProblemQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        areas = services.visible_areas(request.user)
        if data.get("area"):
            areas &= {data["area"]}
        found = services.problems(data["company_code"], on=data.get("on") or timezone.localdate(), areas=areas)
        if data.get("severity"):
            found = [p for p in found if p["severity"] == data["severity"]]
        return Response({"company_code": data["company_code"].code, "areas": sorted(areas), "count": len(found),
                         "critical": sum(p["severity"] == services.CRITICAL for p in found), "problems": found})
