from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("prices", views.SalesPriceViewSet, basename="sales-price")
router.register("orders", views.SalesOrderViewSet, basename="sales-order")
router.register("deliveries", views.DeliveryViewSet, basename="delivery")
router.register("invoices", views.InvoiceViewSet, basename="invoice")

urlpatterns = router.urls
