"""Admin screens of the partner portal (Wave 10a): links of portal users, and what partners told us (read-only)."""
from django.contrib import admin

from .models import DeliveryNotice, ForecastShare, OrderConfirmation, PartnerAccess


@admin.register(PartnerAccess)
class PartnerAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "partner", "kind", "company_code", "created_at")
    list_filter = ("kind",)
    search_fields = ("user__username", "partner__number", "partner__name")


class _ReadOnly(admin.ModelAdmin):
    """What a partner sent is kept as sent: nobody edits or deletes it here."""

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderConfirmation)
class OrderConfirmationAdmin(_ReadOnly):
    list_display = ("order_item", "quantity", "delivery_date", "confirmed_by", "confirmed_at")


@admin.register(DeliveryNotice)
class DeliveryNoticeAdmin(_ReadOnly):
    list_display = ("notice_number", "reference", "supplier", "purchase_order", "arrival_date")


@admin.register(ForecastShare)
class ForecastShareAdmin(_ReadOnly):
    list_display = ("supplier", "active", "agreed_by", "agreed_at", "ended_at")
