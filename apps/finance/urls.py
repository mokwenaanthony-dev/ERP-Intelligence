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
    *router.urls,
]
