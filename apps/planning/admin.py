"""Django admin for planning: forecast runs are read-only (made only by ``services.run_forecast``)."""
from django.contrib import admin

from .models import ForecastItem, ForecastPeriod, ForecastRun


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ForecastRun)
class ForecastRunAdmin(ReadOnlyAdmin):
    list_display = ["run_number", "company_code", "plant", "first_period", "horizon", "history_months",
                    "model_version", "created_at"]
    list_filter = ["plant"]


@admin.register(ForecastItem)
class ForecastItemAdmin(ReadOnlyAdmin):
    list_display = ["run", "material", "method", "mae", "mase", "history_total"]
    list_filter = ["method"]


@admin.register(ForecastPeriod)
class ForecastPeriodAdmin(ReadOnlyAdmin):
    list_display = ["item", "period", "quantity", "lower", "upper"]
