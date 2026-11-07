"""API routes for company codes, plants, storage locations, sales and purchasing organisations,
mounted under ``/api/org/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("company-codes", views.CompanyCodeViewSet, basename="company-code")
router.register("plants", views.PlantViewSet, basename="plant")
router.register("storage-locations", views.StorageLocationViewSet, basename="storage-location")
router.register("sales-organizations", views.SalesOrganizationViewSet, basename="sales-organization")
router.register("purchasing-organizations", views.PurchasingOrganizationViewSet, basename="purchasing-organization")

urlpatterns = router.urls
