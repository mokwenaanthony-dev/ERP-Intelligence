from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import Product


@admin.register(Product)
class ProductAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("sku", "name", "price", "stock", "updated_at")
    search_fields = ("sku", "name")
