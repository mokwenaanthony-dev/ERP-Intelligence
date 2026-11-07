"""Django admin for the refresh log: when each reporting view was last refreshed and how long it took.
"""
from django.contrib import admin

from .models import RefreshLog


@admin.register(RefreshLog)
class RefreshLogAdmin(admin.ModelAdmin):
    list_display = ("view_name", "refreshed_at", "duration_ms")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
