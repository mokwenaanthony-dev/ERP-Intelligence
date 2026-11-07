"""Django admin for the outbox: see pending and failed graph events and the last error of each.
"""
from django.contrib import admin

from .models import OutboxEvent


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ("id", "aggregate_type", "aggregate_id", "created_at", "processed_at", "attempts", "failed")
    list_filter = ("failed", "aggregate_type")
    readonly_fields = [f.name for f in OutboxEvent._meta.fields]

    def has_add_permission(self, request):
        return False
