"""API routes for the general ledger, mounted under ``/api/finance/`` in ``erp_system/urls.py``.

The router builds the list, detail and action URLs from each viewset; the browsable API at each
URL shows the fields and actions it accepts.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("charts-of-accounts", views.ChartOfAccountsViewSet, basename="chart-of-accounts")
router.register("gl-accounts", views.GLAccountViewSet, basename="gl-account")
router.register("tax-codes", views.TaxCodeViewSet, basename="tax-code")
router.register("fiscal-years", views.FiscalYearViewSet, basename="fiscal-year")
router.register("periods", views.PostingPeriodViewSet, basename="posting-period")
router.register("journal-entries", views.JournalEntryViewSet, basename="journal-entry")

urlpatterns = [
    path("reports/trial-balance/", views.TrialBalanceView.as_view(), name="trial-balance"),
    path("incoming-payments/", views.IncomingPaymentView.as_view(), name="incoming-payment"),
    path("reports/open-items/", views.OpenItemsView.as_view(), name="open-items"),
    path("reports/receivables-ageing/", views.ReceivablesAgeingView.as_view(), name="receivables-ageing"),
    path("reports/receivables-reconciliation/", views.ReceivablesReconciliationView.as_view(),
         name="receivables-reconciliation"),
    path("outgoing-payments/", views.OutgoingPaymentView.as_view(), name="outgoing-payment"),
    path("reports/payables-ageing/", views.PayablesAgeingView.as_view(), name="payables-ageing"),
    path("reports/payables-reconciliation/", views.PayablesReconciliationView.as_view(),
         name="payables-reconciliation"),
    *router.urls,
]
