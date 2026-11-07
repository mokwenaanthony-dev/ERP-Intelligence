"""API routes for order-to-cash, mounted under ``/api/sales/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("prices", views.SalesPriceViewSet, basename="sales-price")
router.register("orders", views.SalesOrderViewSet, basename="sales-order")
router.register("deliveries", views.DeliveryViewSet, basename="delivery")
router.register("invoices", views.InvoiceViewSet, basename="invoice")

urlpatterns = router.urls
