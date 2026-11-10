"""API routes for the knowledge graph, mounted under ``/api/graph/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("status/", views.StatusView.as_view(), name="graph-status"),
    path("customer-360/<str:number>/", views.Customer360View.as_view(), name="customer-360"),
    path("document-flow/", views.DocumentFlowView.as_view(), name="document-flow"),
    path("supplier-dependency/", views.SupplierDependencyView.as_view(), name="supplier-dependency"),
    path("where-used/<str:material>/", views.WhereUsedView.as_view(), name="where-used"),
    path("context/", views.ContextView.as_view(), name="graph-context"),
    path("rebuild/", views.RebuildView.as_view(), name="graph-rebuild"),
]
