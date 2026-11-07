"""Django admin screens for goods movements and moving-average stock valuation.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import InventoryAccountSetting, MaterialDocument, MaterialDocumentItem


@admin.register(InventoryAccountSetting)
class InventoryAccountSettingAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("chart_of_accounts", "account_key", "material_type", "gl_account")
    list_filter = ("chart_of_accounts", "account_key")


class MaterialDocumentItemInline(admin.TabularInline):
    model = MaterialDocumentItem
    extra = 0
    can_delete = False
    fields = ("line_number", "material", "storage_location", "quantity", "unit", "amount_local", "text")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MaterialDocument)
class MaterialDocumentAdmin(admin.ModelAdmin):
    """Read-only: goods movements are posted through the API and corrected by reversal."""

    list_display = ("document_number", "company_code", "plant", "movement_type", "posting_date", "status",
                    "journal_entry")
    list_filter = ("company_code", "plant", "movement_type", "status")
    search_fields = ("document_number", "reference")
    inlines = [MaterialDocumentItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
