"""API routes for procure-to-pay, mounted under ``/api/purchasing/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("orders", views.PurchaseOrderViewSet, basename="purchase-order")
router.register("goods-receipts", views.GoodsReceiptViewSet, basename="goods-receipt")
router.register("invoices", views.SupplierInvoiceViewSet, basename="supplier-invoice")

urlpatterns = [
    path("reports/gr-ir/", views.GRIRReportView.as_view(), name="gr-ir-report"),
    *router.urls,
]
