"""API routes for business partners, materials, currencies, units and exchange rates, mounted under
``/api/masterdata/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
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
