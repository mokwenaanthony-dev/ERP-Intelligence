"""API routes for planning, mounted under ``/api/planning/`` in ``erp_system/urls.py``."""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("forecast-runs", views.ForecastRunViewSet, basename="forecast-run")

urlpatterns = [
    path("demand-history/", views.DemandHistoryView.as_view(), name="planning-demand-history"),
    path("forecast/", views.LatestForecastView.as_view(), name="planning-latest-forecast"),
    *router.urls,
]
