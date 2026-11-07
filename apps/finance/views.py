from datetime import date

from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import FinanceConfigPermission, FinancePermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin
from apps.masterdata.models import BusinessPartner
from apps.organization.models import CompanyCode

from . import services
from .models import ChartOfAccounts, FiscalYear, GLAccount, JournalEntry, PostingPeriod, TaxCode
from .serializers import (
    ChartOfAccountsSerializer,
    FiscalYearSerializer,
    GLAccountSerializer,
    IncomingPaymentSerializer,
    OutgoingPaymentSerializer,
    JournalEntryCreateSerializer,
    JournalEntrySerializer,
    PostingPeriodSerializer,
    ReverseSerializer,
    TaxCodeSerializer,
)


def _int_param(request, name, default=None):
    value = request.query_params.get(name, default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise serializers.ValidationError({name: "Must be a whole number."}) from None


# --- Configuration -------------------------------------------------------------------

class _ConfigViewSet(AuditedViewMixin, ProtectedDestroyMixin, viewsets.ModelViewSet):
    permission_classes = [FinanceConfigPermission]


class ChartOfAccountsViewSet(_ConfigViewSet):
    queryset = ChartOfAccounts.objects.all()
    serializer_class = ChartOfAccountsSerializer
    lookup_field = "code"


class GLAccountViewSet(_ConfigViewSet):
    serializer_class = GLAccountSerializer

    def get_queryset(self):
        queryset = GLAccount.objects.select_related("chart_of_accounts")
        chart = self.request.query_params.get("chart_of_accounts")
        return queryset.filter(chart_of_accounts__code=chart) if chart else queryset


class TaxCodeViewSet(_ConfigViewSet):
    queryset = TaxCode.objects.select_related("chart_of_accounts", "gl_account")
    serializer_class = TaxCodeSerializer


class FiscalYearViewSet(AuditedViewMixin, mixins.CreateModelMixin, mixins.ListModelMixin,
                        mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Creating a fiscal year also creates its 12 posting periods."""

    serializer_class = FiscalYearSerializer
    permission_classes = [FinanceConfigPermission]

    def get_queryset(self):
        queryset = FiscalYear.objects.select_related("company_code").prefetch_related("periods")
        company_code = self.request.query_params.get("company_code")
        return queryset.filter(company_code__code=company_code) if company_code else queryset


class PostingPeriodViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = PostingPeriodSerializer
    permission_classes = [FinanceConfigPermission]

    def get_queryset(self):
        queryset = PostingPeriod.objects.select_related("fiscal_year__company_code")
        company_code = self.request.query_params.get("company_code")
        year = _int_param(self.request, "fiscal_year")
        if company_code:
            queryset = queryset.filter(fiscal_year__company_code__code=company_code)
        if year is not None:
            queryset = queryset.filter(fiscal_year__year=year)
        return queryset

    def _set_status(self, request, new_status):
        period = services.set_period_status(self.get_object(), new_status, request.user)
        return Response(self.get_serializer(period).data)

    @action(detail=True, methods=["post"])
    def open(self, request, pk=None):
        return self._set_status(request, PostingPeriod.Status.OPEN)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        return self._set_status(request, PostingPeriod.Status.CLOSED)


# --- Journal entries -----------------------------------------------------------------

class JournalEntryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Post (create), list, view and reverse journal entries. Posted entries are never edited or deleted."""

    serializer_class = JournalEntrySerializer
    permission_classes = [FinancePermission]

    @staticmethod
    def queryset_for_detail():
        return JournalEntry.objects.select_related(
            "company_code", "currency", "reversal_of", "reversed_by"
        ).prefetch_related(
            "lines__gl_account", "lines__currency", "lines__local_currency", "lines__business_partner",
            "lines__material", "lines__plant", "lines__tax_code", "lines__cost_center", "lines__profit_center",
        )

    def get_queryset(self):
        queryset = self.queryset_for_detail()
        params = self.request.query_params
        if params.get("company_code"):
            queryset = queryset.filter(company_code__code=params["company_code"])
        for name in ("fiscal_year", "period"):
            value = _int_param(self.request, name)
            if value is not None:
                queryset = queryset.filter(**{name: value})
        for name in ("document_type", "status"):
            if params.get(name):
                queryset = queryset.filter(**{name: params[name]})
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = JournalEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = serializer.post(request.user)
        output = self.get_serializer(self.get_queryset().get(pk=entry.pk))
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        params = ReverseSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            reversal = services.reverse_journal_entry(
                self.get_object(), posting_date=params.validated_data.get("posting_date"), user=request.user
            )
        except services.PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc
        output = self.get_serializer(self.get_queryset().get(pk=reversal.pk))
        return Response(output.data, status=status.HTTP_201_CREATED)


class TrialBalanceView(APIView):
    """GET /api/finance/reports/trial-balance/?company_code=1000&fiscal_year=2027[&period_from=1&period_to=12]"""

    permission_classes = [FinancePermission]

    def get(self, request):
        code = request.query_params.get("company_code")
        year = _int_param(request, "fiscal_year")
        if not code or year is None:
            raise serializers.ValidationError("company_code and fiscal_year are required.")
        company_code = CompanyCode.objects.select_related("currency").filter(code=code).first()
        if company_code is None:
            raise serializers.ValidationError({"company_code": f"Company code {code} not found."})
        period_from = _int_param(request, "period_from", 1)
        period_to = _int_param(request, "period_to", 12)
        return Response(services.trial_balance(company_code, year, period_from, period_to))


# --- Receivables and payables -----------------------------------------------------------

def _company_code(request) -> CompanyCode:
    company_code = CompanyCode.objects.select_related("currency", "chart_of_accounts").filter(
        code=request.query_params.get("company_code", "")
    ).first()
    if company_code is None:
        raise serializers.ValidationError({"company_code": "A valid company_code is required."})
    return company_code


class IncomingPaymentView(APIView):
    """POST /api/finance/incoming-payments/ - post a customer payment and clear open items."""

    permission_classes = [FinancePermission]
    serializer_class = IncomingPaymentSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = serializer.post(request.user)
        entry = JournalEntryViewSet.queryset_for_detail().get(pk=entry.pk)
        data = JournalEntrySerializer(entry).data
        data["cleared_items"] = [
            {"line_id": c.item_id, "document_number": c.item.entry.document_number, "amount": c.amount}
            for c in entry.clearings_made.select_related("item__entry")
        ]
        return Response(data, status=status.HTTP_201_CREATED)


class OutgoingPaymentView(IncomingPaymentView):
    """POST /api/finance/outgoing-payments/ - pay a supplier and clear its open invoices."""

    serializer_class = OutgoingPaymentSerializer


RECONCILIATION_TYPES = {"customer": GLAccount.ReconciliationType.CUSTOMER,
                        "supplier": GLAccount.ReconciliationType.SUPPLIER}


class OpenItemsView(APIView):
    """GET /api/finance/reports/open-items/?company_code=1000[&type=customer|supplier]
    [&business_partner=1000001][&include_cleared=true]"""

    permission_classes = [FinancePermission]

    def get(self, request):
        company_code = _company_code(request)
        kind = request.query_params.get("type", "customer")
        if kind not in RECONCILIATION_TYPES:
            raise serializers.ValidationError({"type": "Use customer or supplier."})
        partner = None
        if request.query_params.get("business_partner"):
            partner = BusinessPartner.objects.filter(number=request.query_params["business_partner"]).first()
            if partner is None:
                raise serializers.ValidationError({"business_partner": "Business partner not found."})
        include_cleared = request.query_params.get("include_cleared") in ("1", "true", "yes")
        return Response(services.open_items(company_code, reconciliation_type=RECONCILIATION_TYPES[kind],
                                            partner=partner, include_cleared=include_cleared))


class ReceivablesAgeingView(APIView):
    """GET /api/finance/reports/receivables-ageing/?company_code=1000[&as_of=2026-11-30]"""

    permission_classes = [FinancePermission]
    report = staticmethod(services.receivables_ageing)

    def get(self, request):
        company_code = _company_code(request)
        as_of = serializers.DateField().to_internal_value(request.query_params["as_of"]) \
            if request.query_params.get("as_of") else date.today()
        return Response(type(self).report(company_code, as_of))


class PayablesAgeingView(ReceivablesAgeingView):
    """GET /api/finance/reports/payables-ageing/?company_code=1000[&as_of=2026-11-30]"""

    report = staticmethod(services.payables_ageing)


class ReceivablesReconciliationView(APIView):
    """GET /api/finance/reports/receivables-reconciliation/?company_code=1000"""

    permission_classes = [FinancePermission]
    report = staticmethod(services.receivables_reconciliation)

    def get(self, request):
        return Response(type(self).report(_company_code(request)))


class PayablesReconciliationView(ReceivablesReconciliationView):
    """GET /api/finance/reports/payables-reconciliation/?company_code=1000"""

    report = staticmethod(services.payables_reconciliation)
