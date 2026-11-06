from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.permissions import BillingPermission, DeliveryPermission, SalesPermission
from apps.common.views import AuditedViewMixin
from apps.finance.services import PostingError

from . import services
from .models import Delivery, Invoice, SalesOrder, SalesPrice
from .serializers import (
    DeliverSerializer,
    DeliverySerializer,
    InvoiceCreateSerializer,
    InvoiceSerializer,
    PostingDateSerializer,
    SalesOrderCreateSerializer,
    SalesOrderItemsUpdateSerializer,
    SalesOrderSerializer,
    SalesPriceSerializer,
    _item_inputs,
    _rule_error,
)


class SalesPriceViewSet(AuditedViewMixin, viewsets.ModelViewSet):
    serializer_class = SalesPriceSerializer
    permission_classes = [SalesPermission]

    def get_queryset(self):
        queryset = SalesPrice.objects.select_related("material", "sales_organization")
        if self.request.query_params.get("material"):
            queryset = queryset.filter(material__number=self.request.query_params["material"])
        return queryset


class SalesOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Create, list and view orders; change items while OPEN; confirm, cancel and deliver via actions."""

    serializer_class = SalesOrderSerializer
    permission_classes = [SalesPermission]

    def get_queryset(self):
        queryset = SalesOrder.objects.select_related(
            "company_code", "sales_organization", "customer"
        ).prefetch_related("items__material", "items__plant", "items__unit", "items__tax_code", "deliveries")
        params = self.request.query_params
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("customer"):
            queryset = queryset.filter(customer__number=params["customer"])
        return queryset

    def _respond(self, order, code=status.HTTP_200_OK):
        return Response(self.get_serializer(self.get_queryset().get(pk=order.pk)).data, status=code)

    def create(self, request, *args, **kwargs):
        serializer = SalesOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._respond(serializer.post(request.user), status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        """PATCH {"items": [...]} replaces all items while the order is OPEN."""
        order = self.get_object()
        serializer = SalesOrderItemsUpdateSerializer(data=request.data)
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
    def confirm(self, request, pk=None):
        return self._run(request, services.confirm_order)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._run(request, services.cancel_order)

    @action(detail=True, methods=["post"])
    def deliver(self, request, pk=None):
        serializer = DeliverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        delivery = serializer.post(self.get_object(), request.user)
        return Response(DeliverySerializer(DeliveryViewSet.base_queryset().get(pk=delivery.pk)).data,
                        status=status.HTTP_201_CREATED)


class DeliveryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = DeliverySerializer
    permission_classes = [DeliveryPermission]

    @staticmethod
    def base_queryset():
        return Delivery.objects.select_related(
            "sales_order__customer", "plant", "material_document__journal_entry"
        ).prefetch_related("items__order_item__material", "items__storage_location", "invoices")

    def get_queryset(self):
        queryset = self.base_queryset()
        if self.request.query_params.get("sales_order"):
            queryset = queryset.filter(sales_order__order_number=self.request.query_params["sales_order"])
        return queryset

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        params = PostingDateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            delivery = services.reverse_delivery(self.get_object(), user=request.user,
                                                 posting_date=params.validated_data.get("posting_date"))
        except PostingError as exc:
            raise _rule_error(exc) from exc
        return Response(self.get_serializer(self.base_queryset().get(pk=delivery.pk)).data)


class InvoiceViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """POST {"delivery": <id>, "invoice_date": ...} bills a delivery; cancel while unpaid."""

    serializer_class = InvoiceSerializer
    permission_classes = [BillingPermission]

    def get_queryset(self):
        queryset = Invoice.objects.select_related(
            "company_code", "customer", "delivery__sales_order", "journal_entry"
        ).prefetch_related("items__order_item__material", "items__tax_code")
        params = self.request.query_params
        if params.get("customer"):
            queryset = queryset.filter(customer__number=params["customer"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = serializer.post(request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=invoice.pk)).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        params = PostingDateSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        try:
            invoice = services.cancel_invoice(self.get_object(), user=request.user,
                                              posting_date=params.validated_data.get("posting_date"))
        except PostingError as exc:
            raise _rule_error(exc) from exc
        return Response(self.get_serializer(self.get_queryset().get(pk=invoice.pk)).data)
