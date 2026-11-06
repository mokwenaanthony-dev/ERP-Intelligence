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
    *router.urls,
]
