"""API routes for management reporting, mounted under ``/api/analytics/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("income-statement/", views.IncomeStatementView.as_view(), name="income-statement"),
    path("balance-sheet/", views.BalanceSheetView.as_view(), name="balance-sheet"),
    path("sales-margin/", views.SalesMarginView.as_view(), name="sales-margin"),
    path("stock-ageing/", views.StockAgeingView.as_view(), name="stock-ageing"),
    path("payroll-cost/", views.PayrollCostView.as_view(), name="payroll-cost"),
    path("refresh/", views.RefreshView.as_view(), name="analytics-refresh"),
]
