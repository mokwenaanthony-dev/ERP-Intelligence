"""REST API views for CRM, under ``/api/crm/``.

Views stay thin: they check the role (``CRMPermission``: the sales team writes; finance and viewers
read), validate input with a serializer and call ``services.py``. Each status change is an action
(``POST .../<id>/qualify/``), and every one of them is idempotent: the same request again returns the
same result (and the ``Idempotency-Key`` middleware replays the first response, ADR 0010).
"""

from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import CRMPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin
from apps.organization.models import CompanyCode

from . import services
from .models import Activity, Campaign, Lead, Opportunity, Quotation, ServiceTicket
from .serializers import (
    AcceptSerializer,
    ActivityCreateSerializer,
    ActivitySerializer,
    CampaignSerializer,
    CompleteSerializer,
    ConvertSerializer,
    LeadCreateSerializer,
    LeadSerializer,
    LoseSerializer,
    MoveSerializer,
    OpportunityCreateSerializer,
    OpportunitySerializer,
    QuotationCreateSerializer,
    QuotationSerializer,
    ReasonSerializer,
    ResolveSerializer,
    ServiceTicketSerializer,
    TicketCreateSerializer,
    run,
)


def _company(request) -> CompanyCode:
    code = request.query_params.get("company_code", "").strip()
    company = CompanyCode.objects.filter(code=code).first() if code else None
    if company is None:
        raise serializers.ValidationError({"company_code": "Give an existing company code, e.g. company_code=1000."})
    return company


class _DocumentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List, view and create a CRM document; status changes are actions that call a service."""

    permission_classes = [CRMPermission]
    create_serializer = None
    filters = {}  # query parameter -> queryset filter

    def filter_queryset(self, queryset):
        params = self.request.query_params
        for param, lookup in self.filters.items():
            if params.get(param):
                queryset = queryset.filter(**{lookup: params[param]})
        return queryset

    def _respond(self, obj, code=status.HTTP_200_OK):
        return Response(self.get_serializer(self.get_queryset().get(pk=obj.pk)).data, status=code)

    def create(self, request, *args, **kwargs):
        serializer = self.create_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._respond(serializer.post(request.user), status.HTTP_201_CREATED)

    def _act(self, request, func, params_class=None, **fixed):
        params = {}
        if params_class is not None:
            serializer = params_class(data=request.data)
            serializer.is_valid(raise_exception=True)
            params = serializer.validated_data
        return self._respond(run(func, self.get_object(), user=request.user, **fixed, **params))


class CampaignViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    serializer_class = CampaignSerializer
    permission_classes = [CRMPermission]

    def get_queryset(self):
        queryset = Campaign.objects.select_related("company_code")
        if self.request.query_params.get("company_code"):
            queryset = queryset.filter(company_code__code=self.request.query_params["company_code"])
        return queryset


class LeadViewSet(_DocumentViewSet):
    """Leads: create, then qualify, disqualify (with a reason) or convert (customer + opportunity)."""

    serializer_class = LeadSerializer
    create_serializer = LeadCreateSerializer
    filters = {"status": "status", "company_code": "company_code__code", "campaign": "campaign__code"}

    def get_queryset(self):
        return Lead.objects.select_related("company_code", "campaign", "owner", "business_partner", "opportunity")

    @action(detail=True, methods=["post"])
    def qualify(self, request, pk=None):
        return self._act(request, services.qualify_lead)

    @action(detail=True, methods=["post"])
    def disqualify(self, request, pk=None):
        return self._act(request, services.disqualify_lead, ReasonSerializer)

    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        """POST {"expected_close_date", "expected_value", "title", "customer"?}: the opportunity is returned."""
        params = ConvertSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        opportunity = run(services.convert_lead, self.get_object(), user=request.user, **params.validated_data)
        return Response(OpportunitySerializer(opportunity, context={"request": request}).data,
                        status=status.HTTP_200_OK)


class OpportunityViewSet(_DocumentViewSet):
    """Opportunities: create for a customer, move between open stages, lose with a reason. Won by
    accepting a quotation."""

    serializer_class = OpportunitySerializer
    create_serializer = OpportunityCreateSerializer
    filters = {"stage": "stage", "company_code": "company_code__code", "customer": "customer__number"}

    def get_queryset(self):
        return Opportunity.objects.select_related("company_code", "customer", "campaign", "owner") \
            .prefetch_related("quotations")

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        params = MoveSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self._respond(run(services.move_opportunity, self.get_object(), params.validated_data["stage"],
                                 probability=params.validated_data.get("probability"), user=request.user))

    @action(detail=True, methods=["post"])
    def lose(self, request, pk=None):
        return self._act(request, services.lose_opportunity, LoseSerializer)


class QuotationViewSet(_DocumentViewSet):
    """Quotations: create (priced from the price list), send, accept (creates the sales order), reject."""

    serializer_class = QuotationSerializer
    create_serializer = QuotationCreateSerializer
    filters = {"status": "status", "company_code": "company_code__code", "customer": "customer__number"}

    def get_queryset(self):
        return Quotation.objects.select_related(
            "company_code", "sales_organization", "customer", "opportunity", "sales_order"
        ).prefetch_related("items__material")

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        return self._act(request, services.send_quotation)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        return self._act(request, services.accept_quotation, AcceptSerializer)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._act(request, services.reject_quotation)


class ServiceTicketViewSet(_DocumentViewSet):
    """Service tickets: open (due time from the priority), start, resolve, close, reopen."""

    serializer_class = ServiceTicketSerializer
    create_serializer = TicketCreateSerializer
    filters = {"status": "status", "company_code": "company_code__code", "customer": "customer__number",
               "priority": "priority"}

    def get_queryset(self):
        return ServiceTicket.objects.select_related("company_code", "customer", "sales_order", "material", "owner")

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        return self._act(request, services.start_ticket)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        return self._act(request, services.resolve_ticket, ResolveSerializer)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        return self._act(request, services.close_ticket)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._act(request, services.reopen_ticket)


class ActivityViewSet(_DocumentViewSet):
    """Activities about a lead, an opportunity or a ticket; ``?open=true`` lists the ones not done yet."""

    serializer_class = ActivitySerializer
    create_serializer = ActivityCreateSerializer
    filters = {"kind": "kind"}

    def get_queryset(self):
        queryset = Activity.objects.select_related("lead", "opportunity", "ticket", "owner")
        if self.request.query_params.get("open") == "true":
            queryset = queryset.filter(completed_on__isnull=True)
        return queryset

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        return self._act(request, services.complete_activity, CompleteSerializer)


class PipelineView(APIView):
    """GET /api/crm/pipeline/?company_code=1000 - open opportunities by stage, weighted forecast by month,
    win rate."""

    permission_classes = [CRMPermission]

    def get(self, request):
        return Response(services.pipeline(_company(request)))


class CampaignPerformanceView(APIView):
    """GET /api/crm/campaign-performance/?company_code=1000 - leads, conversions, won value and return per
    campaign."""

    permission_classes = [CRMPermission]

    def get(self, request):
        company = _company(request)
        return Response({"company_code": company.code, "campaigns": services.campaign_performance(company)})


class OverdueTicketsView(APIView):
    """GET /api/crm/overdue-tickets/?company_code=1000 - tickets past their due time, most overdue first."""

    permission_classes = [CRMPermission]

    def get(self, request):
        company = _company(request)
        now = timezone.now()
        tickets = services.overdue_tickets(company, now)
        rows = ServiceTicketSerializer(tickets, many=True, context={"request": request}).data
        for row, ticket in zip(rows, tickets):
            row["hours_overdue"] = round((now - ticket.due_at).total_seconds() / 3600, 1)
        return Response({"company_code": company.code, "as_of": now, "tickets": rows})
