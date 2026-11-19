"""Admin screens of side-by-side mode (Wave 10b): what was loaded, read-only."""
from django.contrib import admin

from .models import ExternalReference, ImportBatch


class _ReadOnly(admin.ModelAdmin):
    """Imports are a record of what happened: nobody edits or deletes them here."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImportBatch)
class ImportBatchAdmin(_ReadOnly):
    list_display = ("imported_at", "source", "object_type", "rows", "created", "updated", "unchanged", "rejected")
    list_filter = ("source", "object_type")


@admin.register(ExternalReference)
class ExternalReferenceAdmin(_ReadOnly):
    list_display = ("source", "object_type", "external_key", "number", "updated_at")
    list_filter = ("source", "object_type")
    search_fields = ("external_key", "number")
