from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import GoodsReceiptPermission, PurchasingPermission, SupplierInvoicePermission
from apps.finance.services import PostingError
from apps.finance.views import _company_code

from . import services
from .models import GoodsReceipt, PurchaseOrder, SupplierInvoice
from .serializers import (
    GoodsReceiptSerializer,
    PostingDateSerializer,
    PurchaseOrderCreateSerializer,
    PurchaseOrderItemsUpdateSerializer,
    PurchaseOrderSerializer,
    ReceiveSerializer,
    SupplierInvoiceCreateSerializer,
    SupplierInvoiceSerializer,
    _item_inputs,
    _rule_error,
)


class PurchaseOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Create, list and view orders; change items while OPEN; release and cancel via actions."""

    serializer_class = PurchaseOrderSerializer
    permission_classes = [PurchasingPermission]

    def get_queryset(self):
        queryset = PurchaseOrder.objects.select_related(
            "company_code", "purchasing_organization", "supplier"
        ).prefetch_related("items__material", "items__plant", "items__storage_location", "items__unit",
                           "items__tax_code", "receipts", "invoices")
        params = self.request.query_params
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("supplier"):
            queryset = queryset.filter(supplier__number=params["supplier"])
        return queryset

    def _respond(self, order, code=status.HTTP_200_OK):
        return Response(self.get_serializer(self.get_queryset().get(pk=order.pk)).data, status=code)

    def create(self, request, *args, **kwargs):
        serializer = PurchaseOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._respond(serializer.post(request.user), status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        """PATCH {"items": [...]} replaces all items while the order is OPEN."""
        order = self.get_object()
        serializer = PurchaseOrderItemsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = _item_inputs(serializer.validated_data["items"], order.company_code)
        try:
            services.replace_order_items(order, items, request.user)
        except PostingError as exc:
            raise _rule_error(exc) from exc
        return self._respond(order)

    def _run(self, request, func):
        try:
            order = func(self.get_object(), user=request.user)
        except PostingError as exc:
            raise _rule_error(exc) from exc
        return self._respond(order)

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        return self._run(request, services.release_order)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._run(request, services.cancel_order)


class GoodsReceiptViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """POST {"purchase_order": "PO00000001", "receipt_date": ...} receives goods; reverse via action."""

    serializer_class = GoodsReceiptSerializer
    permission_classes = [GoodsReceiptPermission]

    def get_queryset(self):
        queryset = GoodsReceipt.objects.select_related(
            "purchase_order__supplier", "plant", "material_document__journal_entry"
        ).prefetch_related("items__order_item__material", "items__storage_location")
        if self.request.query_params.get("purchase_order"):
            queryset = queryset.filter(purchase_order__order_number=self.request.query_params["purchase_order"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = ReceiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = serializer.post(request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=receipt.pk)).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        params = PostingDateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            receipt = services.reverse_goods_receipt(self.get_object(), user=request.user,
                                                     posting_date=params.validated_data.get("posting_date"))
        except PostingError as exc:
            raise _rule_error(exc) from exc
        return Response(self.get_serializer(self.get_queryset().get(pk=receipt.pk)).data)


class SupplierInvoiceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """POST {"purchase_order": ..., "supplier_reference": ..., "invoice_date": ...} posts a matched
    supplier invoice; cancel while unpaid."""

    serializer_class = SupplierInvoiceSerializer
    permission_classes = [SupplierInvoicePermission]

    def get_queryset(self):
        queryset = SupplierInvoice.objects.select_related(
            "company_code", "supplier", "purchase_order", "journal_entry"
        ).prefetch_related("items__order_item__material", "items__tax_code")
        params = self.request.query_params
        if params.get("supplier"):
            queryset = queryset.filter(supplier__number=params["supplier"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = SupplierInvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = serializer.post(request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=invoice.pk)).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        params = PostingDateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            invoice = services.cancel_supplier_invoice(self.get_object(), user=request.user,
                                                       posting_date=params.validated_data.get("posting_date"))
        except PostingError as exc:
            raise _rule_error(exc) from exc
        return Response(self.get_serializer(self.get_queryset().get(pk=invoice.pk)).data)


class GRIRReportView(APIView):
    """GET /api/purchasing/reports/gr-ir/?company_code=1000 - goods received not yet invoiced."""

    permission_classes = [SupplierInvoicePermission]

    def get(self, request):
        return Response(services.gr_ir_report(_company_code(request)))
