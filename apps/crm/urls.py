"""API routes for CRM, mounted under ``/api/crm/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("campaigns", views.CampaignViewSet, basename="campaign")
router.register("leads", views.LeadViewSet, basename="lead")
router.register("opportunities", views.OpportunityViewSet, basename="opportunity")
router.register("quotations", views.QuotationViewSet, basename="quotation")
router.register("tickets", views.ServiceTicketViewSet, basename="service-ticket")
router.register("activities", views.ActivityViewSet, basename="activity")

urlpatterns = [
    path("pipeline/", views.PipelineView.as_view(), name="crm-pipeline"),
    path("campaign-performance/", views.CampaignPerformanceView.as_view(), name="crm-campaign-performance"),
    path("overdue-tickets/", views.OverdueTicketsView.as_view(), name="crm-overdue-tickets"),
    *router.urls,
]
