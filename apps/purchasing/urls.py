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
