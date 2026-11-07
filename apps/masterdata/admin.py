"""Django admin screens for business partners, materials, currencies, units and exchange rates.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import (
    BusinessPartner,
    Currency,
    CustomerRole,
    ExchangeRate,
    Material,
    MaterialPlant,
    SupplierRole,
    UnitOfMeasure,
)


class CustomerRoleInline(admin.TabularInline):
    model = CustomerRole
    extra = 0
    fields = ("company_code", "reconciliation_account", "payment_terms_days", "credit_limit")


class SupplierRoleInline(admin.TabularInline):
    model = SupplierRole
    extra = 0
    fields = ("company_code", "reconciliation_account", "payment_terms_days")


@admin.register(BusinessPartner)
class BusinessPartnerAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "name", "category", "vat_number", "is_blocked")
    search_fields = ("number", "name", "vat_number")
    readonly_fields = AuditedAdminMixin.readonly_fields + ("number",)
    inlines = [CustomerRoleInline, SupplierRoleInline]


@admin.register(Material)
class MaterialAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "description", "material_type", "base_unit", "is_active")
    list_filter = ("material_type", "is_active")
    search_fields = ("number", "description")


@admin.register(Currency, UnitOfMeasure)
class CodeNameAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name")


@admin.register(ExchangeRate)
class ExchangeRateAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("from_currency", "to_currency", "rate", "valid_from")


@admin.register(MaterialPlant)
class MaterialPlantAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("material", "plant")
    list_filter = ("plant",)
