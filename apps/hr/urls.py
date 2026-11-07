"""API routes for HR and payroll, mounted under ``/api/hr/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("org-units", views.OrgUnitViewSet, basename="org-unit")
router.register("positions", views.PositionViewSet, basename="position")
router.register("employees", views.EmployeeViewSet, basename="employee")
router.register("payroll-runs", views.PayrollRunViewSet, basename="payroll-run")

urlpatterns = router.urls
