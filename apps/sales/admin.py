"""Django admin screens for order-to-cash.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import (
    Delivery,
    DeliveryItem,
    Invoice,
    InvoiceItem,
    RevenueAccountSetting,
    SalesOrder,
    SalesOrderItem,
    SalesPrice,
)


@admin.register(SalesPrice)
class SalesPriceAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("material", "sales_organization", "price", "valid_from")
    list_filter = ("sales_organization",)


@admin.register(RevenueAccountSetting)
class RevenueAccountSettingAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("chart_of_accounts", "material_type", "revenue_account", "default_tax_code")


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class SalesOrderItemInline(_ReadOnlyInline):
    model = SalesOrderItem
    fields = ("item_number", "material", "plant", "quantity", "unit", "net_price", "tax_code")


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Sales documents are created and changed through the API so the business rules always apply."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SalesOrder)
class SalesOrderAdmin(_ReadOnlyAdmin):
    list_display = ("order_number", "customer", "order_date", "status")
    list_filter = ("status", "sales_organization")
    search_fields = ("order_number", "customer__name", "customer_reference")
    inlines = [SalesOrderItemInline]


class DeliveryItemInline(_ReadOnlyInline):
    model = DeliveryItem
    fields = ("order_item", "storage_location", "quantity")


@admin.register(Delivery)
class DeliveryAdmin(_ReadOnlyAdmin):
    list_display = ("delivery_number", "sales_order", "delivery_date", "status", "material_document")
    list_filter = ("status",)
    inlines = [DeliveryItemInline]


class InvoiceItemInline(_ReadOnlyInline):
    model = InvoiceItem
    fields = ("order_item", "quantity", "net_price", "net_amount", "tax_code", "tax_amount")


@admin.register(Invoice)
class InvoiceAdmin(_ReadOnlyAdmin):
    list_display = ("invoice_number", "customer", "invoice_date", "due_date", "gross_amount", "status")
    list_filter = ("status",)
    search_fields = ("invoice_number", "customer__name")
    inlines = [InvoiceItemInline]
