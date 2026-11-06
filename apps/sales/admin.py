from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import Customer, SalesOrder, SalesOrderLine


class SalesOrderLineInline(admin.TabularInline):
    model = SalesOrderLine
    extra = 1
    fields = ("product", "quantity", "unit_price")
    readonly_fields = ("unit_price",)


@admin.register(SalesOrder)
class SalesOrderAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "customer", "status", "order_date", "total")
    list_filter = ("status",)
    # Status changes go through the API so stock rules always apply.
    readonly_fields = AuditedAdminMixin.readonly_fields + ("status",)
    inlines = [SalesOrderLineInline]


@admin.register(Customer)
class CustomerAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "email", "phone")
    search_fields = ("name", "email")
