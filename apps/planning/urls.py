"""API routes for planning, mounted under ``/api/planning/`` in ``erp_system/urls.py``."""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("forecast-runs", views.ForecastRunViewSet, basename="forecast-run")
router.register("material-planning", views.MaterialPlanningViewSet, basename="material-planning")
router.register("mrp-runs", views.MRPRunViewSet, basename="mrp-run")
router.register("planned-orders", views.PlannedOrderViewSet, basename="planned-order")

urlpatterns = [
    path("demand-history/", views.DemandHistoryView.as_view(), name="planning-demand-history"),
    path("forecast/", views.LatestForecastView.as_view(), name="planning-latest-forecast"),
    *router.urls,
]
