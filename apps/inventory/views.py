from rest_framework import generics

from apps.accounts.permissions import InventoryPermission
from apps.common.views import AuditedViewMixin, ProtectedDestroyMixin

from .models import Product
from .serializers import ProductSerializer


class ProductList(AuditedViewMixin, generics.ListCreateAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [InventoryPermission]


class ProductDetail(AuditedViewMixin, ProtectedDestroyMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [InventoryPermission]
