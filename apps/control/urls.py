"""API routes of the control tower, mounted under ``/api/control/`` in ``erp_system/urls.py``."""
from django.urls import path

from . import views

urlpatterns = [
    path("kpis/", views.KPIView.as_view(), name="control-kpis"),
    path("problems/", views.ProblemView.as_view(), name="control-problems"),
    path("recommendations/", views.RecommendationView.as_view(), name="control-recommendations"),
    path("decisions/", views.DecisionView.as_view(), name="control-decisions"),
    path("acceptance/", views.AcceptanceView.as_view(), name="control-acceptance"),
    path("assistant/", views.AssistantView.as_view(), name="control-assistant"),
    path("explain/", views.ExplainView.as_view(), name="control-explain"),
]
