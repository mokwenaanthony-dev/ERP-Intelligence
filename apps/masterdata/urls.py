from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("currencies", views.CurrencyViewSet, basename="currency")
router.register("exchange-rates", views.ExchangeRateViewSet, basename="exchange-rate")
router.register("units", views.UnitOfMeasureViewSet, basename="unit")
router.register("business-partners", views.BusinessPartnerViewSet, basename="business-partner")
router.register("customer-roles", views.CustomerRoleViewSet, basename="customer-role")
router.register("supplier-roles", views.SupplierRoleViewSet, basename="supplier-role")
router.register("materials", views.MaterialViewSet, basename="material")

urlpatterns = router.urls
