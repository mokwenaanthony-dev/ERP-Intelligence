"""Django admin screens for procure-to-pay.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchasingAccountSetting,
    SupplierInvoice,
    SupplierInvoiceItem,
)


@admin.register(PurchasingAccountSetting)
class PurchasingAccountSettingAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("chart_of_accounts", "default_tax_code", "price_difference_account", "price_tolerance_percent")


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Purchasing documents are created and changed through the API so the business rules always apply."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PurchaseOrderItemInline(_ReadOnlyInline):
    model = PurchaseOrderItem
    fields = ("item_number", "material", "plant", "storage_location", "quantity", "unit", "net_price", "tax_code")


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(_ReadOnlyAdmin):
    list_display = ("order_number", "supplier", "order_date", "status")
    list_filter = ("status", "purchasing_organization")
    search_fields = ("order_number", "supplier__name", "supplier_reference")
    inlines = [PurchaseOrderItemInline]


class GoodsReceiptItemInline(_ReadOnlyInline):
    model = GoodsReceiptItem
    fields = ("order_item", "storage_location", "quantity", "value")


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(_ReadOnlyAdmin):
    list_display = ("receipt_number", "purchase_order", "receipt_date", "status", "material_document")
    list_filter = ("status",)
    inlines = [GoodsReceiptItemInline]


class SupplierInvoiceItemInline(_ReadOnlyInline):
    model = SupplierInvoiceItem
    fields = ("order_item", "quantity", "net_price", "net_amount", "gr_ir_amount", "price_difference",
              "tax_code", "tax_amount")


@admin.register(SupplierInvoice)
class SupplierInvoiceAdmin(_ReadOnlyAdmin):
    list_display = ("invoice_number", "supplier_reference", "supplier", "invoice_date", "due_date", "gross_amount",
                    "status")
    list_filter = ("status",)
    search_fields = ("invoice_number", "supplier_reference", "supplier__name")
    inlines = [SupplierInvoiceItemInline]
