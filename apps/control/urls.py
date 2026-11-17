"""API routes of the control tower, mounted under ``/api/control/`` in ``erp_system/urls.py``."""
from django.urls import path

from . import views

urlpatterns = [
    path("kpis/", views.KPIView.as_view(), name="control-kpis"),
    path("problems/", views.ProblemView.as_view(), name="control-problems"),
]
