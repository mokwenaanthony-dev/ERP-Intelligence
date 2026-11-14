"""REST API views for SRM, under ``/api/srm/``.

Views stay thin: they check the role (``SRMPermission``: purchasing writes; finance, the warehouse
and viewers read), validate input and call ``services.py``. Status changes are actions
(``POST .../<id>/approve/``), all idempotent; the ``Idempotency-Key`` middleware also replays the
first response to a retried request (ADR 0010).
"""
from decimal import Decimal

from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import SRMPermission
from apps.organization.models import CompanyCode

from . import services
from .models import Contract, RequestForQuotation, SupplierApplication, SupplierEvaluation
from .serializers import (
    ApplicationCreateSerializer,
    ApplicationSerializer,
    AwardSerializer,
    ContractCreateSerializer,
    ContractSerializer,
    EvaluateSerializer,
    EvaluationSerializer,
    QuoteSubmitSerializer,
    ReasonSerializer,
    ReleaseSerializer,
    RFQCreateSerializer,
    RFQSerializer,
    run,
)


CENT = Decimal("0.01")


def _company(request) -> CompanyCode:
    code = request.query_params.get("company_code", "").strip()
    company = CompanyCode.objects.filter(code=code).first() if code else None
    if company is None:
        raise serializers.ValidationError({"company_code": "Give an existing company code, e.g. company_code=1000."})
    return company


class _DocumentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List, view and create an SRM document; status changes are actions that call a service."""

    permission_classes = [SRMPermission]
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


class ApplicationViewSet(_DocumentViewSet):
    """Supplier applications: submit, then approve (creates the supplier) or reject with a reason."""

    serializer_class = ApplicationSerializer
    create_serializer = ApplicationCreateSerializer
    filters = {"status": "status", "company_code": "company_code__code"}

    def get_queryset(self):
        return SupplierApplication.objects.select_related("company_code", "business_partner")

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._respond(run(services.approve_application, self.get_object(), user=request.user))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        params = ReasonSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self._respond(run(services.reject_application, self.get_object(), params.validated_data["reason"],
                                 user=request.user))


class RFQViewSet(_DocumentViewSet):
    """Requests for quotation: create (items and invited suppliers), send, record each supplier's quote,
    compare, award (creates the purchase order) or cancel."""

    serializer_class = RFQSerializer
    create_serializer = RFQCreateSerializer
    filters = {"status": "status", "company_code": "company_code__code"}

    def get_queryset(self):
        return RequestForQuotation.objects.select_related(
            "company_code", "purchasing_organization", "purchase_order"
        ).prefetch_related("quotes__supplier", "quotes__items__rfq_item")

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        return self._respond(run(services.send_rfq, self.get_object(), user=request.user))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._respond(run(services.cancel_rfq, self.get_object(), user=request.user))

    def _quote_of(self, rfq, supplier):
        quote = rfq.quotes.filter(supplier=supplier).first()
        if quote is None:
            raise serializers.ValidationError({"supplier": f"Supplier {supplier.number} was not invited to "
                                                           f"{rfq.rfq_number}."})
        return quote

    @action(detail=True, methods=["post"])
    def quote(self, request, pk=None):
        """POST {"supplier", "prices": {"TG-1001": "185.00"}, "delivery_days", "submitted_on"}."""
        rfq = self.get_object()
        params = QuoteSubmitSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        data = dict(params.validated_data)
        quote = self._quote_of(rfq, data.pop("supplier"))
        run(services.submit_quote, quote, user=request.user, **data)
        return self._respond(rfq)

    @action(detail=True, methods=["get"])
    def comparison(self, request, pk=None):
        """The quotes side by side, cheapest first, and the lowest price per item."""
        return Response(services.compare_quotes(self.get_object()))

    @action(detail=True, methods=["post"])
    def award(self, request, pk=None):
        """POST {"supplier", "on"}: the purchase order is created at that supplier's quoted prices."""
        rfq = self.get_object()
        params = AwardSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        quote = self._quote_of(rfq, params.validated_data["supplier"])
        return self._respond(run(services.award_rfq, rfq, quote, on=params.validated_data["on"], user=request.user))


class ContractViewSet(_DocumentViewSet):
    """Contracts: create (agreed prices and target quantities), release purchase orders, cancel."""

    serializer_class = ContractSerializer
    create_serializer = ContractCreateSerializer
    filters = {"status": "status", "company_code": "company_code__code", "supplier": "supplier__number"}

    def get_queryset(self):
        return Contract.objects.select_related("company_code", "purchasing_organization", "supplier") \
            .prefetch_related("items__material", "releases__purchase_order")

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        """POST {"order_date", "items": [{"material", "quantity"}][, "delivery_date"]}: returns the order number."""
        params = ReleaseSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        order = run(services.release_order, self.get_object(), order_date=data["order_date"],
                    delivery_date=data["delivery_date"],
                    items=[(i["material"], i["quantity"]) for i in data["items"]], user=request.user)
        total = sum((item.net_price * item.quantity for item in order.items.all()), Decimal("0")).quantize(CENT)
        return Response({"purchase_order": order.order_number, "contract": order.supplier_reference,
                         "net_total": total}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._respond(run(services.cancel_contract, self.get_object(), user=request.user))


class EvaluationViewSet(_DocumentViewSet):
    """Supplier evaluations: POST evaluates a supplier for a period (again: replaces that period)."""

    serializer_class = EvaluationSerializer
    create_serializer = EvaluateSerializer
    filters = {"supplier": "supplier__number", "company_code": "company_code__code", "grade": "grade"}

    def get_queryset(self):
        return SupplierEvaluation.objects.select_related("company_code", "supplier", "evaluated_by")


class ScorecardView(APIView):
    """GET /api/srm/scorecard/?company_code=1000 - every supplier with its latest evaluation and grade."""

    permission_classes = [SRMPermission]

    def get(self, request):
        company = _company(request)
        return Response({"company_code": company.code, "suppliers": services.scorecard(company)})
