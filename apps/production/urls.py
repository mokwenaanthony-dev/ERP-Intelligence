"""API routes for production planning, mounted under ``/api/production/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("areas", views.ProductionAreaViewSet, basename="production-area")
router.register("lines", views.ProductionLineViewSet, basename="production-line")
router.register("work-centers", views.WorkCenterViewSet, basename="work-center")
router.register("activity-types", views.ActivityTypeViewSet, basename="activity-type")
router.register("activity-rates", views.ActivityRateViewSet, basename="activity-rate")
router.register("boms", views.BOMViewSet, basename="bom")
router.register("routings", views.RoutingViewSet, basename="routing")

urlpatterns = [
    path("equipment/", views.EquipmentView.as_view(), name="equipment"),
    path("cost-estimate/", views.CostEstimateView.as_view(), name="cost-estimate"),
    *router.urls,
]
