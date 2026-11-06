from rest_framework import generics

from apps.accounts.permissions import SalesPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin

from .models import Customer, SalesOrder
from .serializers import CustomerSerializer, SalesOrderSerializer


class CustomerList(AuditedViewMixin, generics.ListCreateAPIView):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [SalesPermission]


class CustomerDetail(AuditedViewMixin, ProtectedDestroyMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [SalesPermission]


def _orders():
    return SalesOrder.objects.select_related("customer").prefetch_related("lines__product")


class SalesOrderList(AuditedViewMixin, generics.ListCreateAPIView):
    serializer_class = SalesOrderSerializer
    permission_classes = [SalesPermission]

    def get_queryset(self):
        return _orders()


class SalesOrderDetail(AuditedViewMixin, generics.RetrieveUpdateAPIView):
    """No DELETE: orders are business documents. Set status to CANCELLED instead."""

    serializer_class = SalesOrderSerializer
    permission_classes = [SalesPermission]

    def get_queryset(self):
        return _orders()
