"""REST API of the control tower (Waves 9a and 9b), under ``/api/control/``.

    GET  kpis/?company_code=1000[&on=YYYY-MM-DD][&days=90][&months=3]   KPIs now and ahead, with status
    GET  problems/?company_code=1000[&on=][&area=][&severity=]           the problems inbox, filtered by role
    GET  recommendations/?company_code=1000[&on=][&area=][&severity=][&status=open|accepted|rejected]
    POST decisions/   {company_code, key, outcome: ACCEPTED|REJECTED, reason}   201 recorded, 200 repeated,
                      409 a different decision exists, 403 not the owner
    GET  decisions/?company_code=1000[&kind=][&outcome=]                the decision log
    GET  acceptance/?company_code=1000[&days=90]                         track record per kind
    GET  assistant/                                                      is the AI assistant on, and why
    POST assistant/   {enabled: true|false}                              administrators only
    POST explain/     {company_code, key[, question][, on]}              grounded facts, and the AI answer if on

Every role except HR may read (``ControlPermission``); the inbox shows only the areas the user's
role may see in the rest of the API. Only the owner of a problem decides it (``decisions``).
"""
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import (AssistantAdminPermission, ControlPermission, DecisionPermission,
                                       ExplainPermission)
from apps.organization.models import CompanyCode

from . import assistant, decisions, services
from .models import Decision


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


class RecommendationQuery(ProblemQuery):
    status = serializers.ChoiceField(choices=decisions.STATUSES, required=False)


class DecisionInput(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    key = serializers.CharField(max_length=120)
    outcome = serializers.ChoiceField(choices=Decision.Outcome.choices)
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    on = serializers.DateField(required=False)


class DecisionQuery(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    kind = serializers.CharField(max_length=30, required=False)
    outcome = serializers.ChoiceField(choices=Decision.Outcome.choices, required=False)


class AcceptanceQuery(_Query):
    days = serializers.IntegerField(min_value=1, max_value=730, default=90)


class ExplainInput(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.select_related(
        "currency"))
    key = serializers.CharField(max_length=120)
    question = serializers.CharField(max_length=assistant.MAX_QUESTION, required=False, allow_blank=True,
                                     default="")
    on = serializers.DateField(required=False)


class SwitchInput(serializers.Serializer):
    enabled = serializers.BooleanField()


def _call(func, *args, **kwargs):
    """Run a decision or assistant service: broken rule 400, conflict 409, not the owner 403."""
    try:
        return func(*args, **kwargs), None
    except decisions.DecisionConflict as exc:
        return None, Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except decisions.NotOwner as exc:
        raise PermissionDenied(str(exc)) from exc
    except decisions.DecisionError as exc:
        raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


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


class RecommendationView(APIView):
    permission_classes = [ControlPermission]

    def get(self, request):
        query = RecommendationQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        areas = services.visible_areas(request.user)
        if data.get("area"):
            areas &= {data["area"]}
        rows = decisions.recommendations(data["company_code"], on=data.get("on") or timezone.localdate(),
                                         areas=areas, status=data.get("status"))
        if data.get("severity"):
            rows = [r for r in rows if r["severity"] == data["severity"]]
        for row in rows:
            row["may_decide"] = decisions.may_decide(request.user, row)
        return Response({"company_code": data["company_code"].code, "areas": sorted(areas), "count": len(rows),
                         "recommendations": rows})


class DecisionView(APIView):
    permission_classes = [DecisionPermission]

    def get(self, request):
        query = DecisionQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        rows = Decision.objects.filter(company_code=data["company_code"],
                                       area__in=services.visible_areas(request.user)).select_related("decided_by")
        for field in ("kind", "outcome"):
            if data.get(field):
                rows = rows.filter(**{field: data[field]})
        found = [decisions.as_dict(d) for d in rows[:500]]
        return Response({"company_code": data["company_code"].code, "count": len(found), "decisions": found})

    def post(self, request):
        given = DecisionInput(data=request.data)
        given.is_valid(raise_exception=True)
        data = given.validated_data
        result, refused = _call(decisions.decide, data["company_code"], key=data["key"], outcome=data["outcome"],
                                reason=data["reason"], user=request.user, on=data.get("on") or timezone.localdate())
        if refused:
            return refused
        decision, created = result
        return Response(decisions.as_dict(decision), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AcceptanceView(APIView):
    permission_classes = [ControlPermission]

    def get(self, request):
        query = AcceptanceQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        return Response(decisions.acceptance(data["company_code"], on=data.get("on") or timezone.localdate(),
                                             days=data["days"]))


class AssistantView(APIView):
    permission_classes = [AssistantAdminPermission]

    def get(self, request):
        return Response(assistant.status())

    def post(self, request):
        given = SwitchInput(data=request.data)
        given.is_valid(raise_exception=True)
        result, _ = _call(assistant.switch, given.validated_data["enabled"], user=request.user)
        return Response(result)


class ExplainView(APIView):
    permission_classes = [ExplainPermission]

    def post(self, request):
        given = ExplainInput(data=request.data)
        given.is_valid(raise_exception=True)
        data = given.validated_data
        result, _ = _call(assistant.explain, data["company_code"], key=data["key"], user=request.user,
                          on=data.get("on") or timezone.localdate(), question=data["question"])
        return Response(result)
