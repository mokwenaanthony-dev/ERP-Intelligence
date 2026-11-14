"""API routes for SRM, mounted under ``/api/srm/`` in ``erp_system/urls.py``."""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("applications", views.ApplicationViewSet, basename="supplier-application")
router.register("rfqs", views.RFQViewSet, basename="rfq")
router.register("contracts", views.ContractViewSet, basename="contract")
router.register("evaluations", views.EvaluationViewSet, basename="supplier-evaluation")

urlpatterns = [
    path("scorecard/", views.ScorecardView.as_view(), name="srm-scorecard"),
    *router.urls,
]
