"""Top-level URL map: the health check, JWT sign-in, refresh and logout, the admin, and each app's API.
"""
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.views import LogoutView
from apps.common.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),

    # JWT authentication (design document, section 3)
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/auth/logout/", LogoutView.as_view(), name="token_blacklist"),  # repeatable (ADR 0010)

    # ERP modules
    path("api/org/", include("apps.organization.urls")),
    path("api/masterdata/", include("apps.masterdata.urls")),
    path("api/finance/", include("apps.finance.urls")),
    path("api/inventory/", include("apps.inventory.urls")),
    path("api/sales/", include("apps.sales.urls")),
    path("api/purchasing/", include("apps.purchasing.urls")),
    path("api/hr/", include("apps.hr.urls")),
    path("api/controlling/", include("apps.controlling.urls")),
    path("api/analytics/", include("apps.analytics.urls")),
    path("api/graph/", include("apps.graph.urls")),
    path("api/production/", include("apps.production.urls")),
    path("api/crm/", include("apps.crm.urls")),
]
