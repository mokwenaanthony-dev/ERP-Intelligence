"""Django admin screens for company codes, plants, storage locations, sales and purchasing organisations.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import CompanyCode, Plant, PurchasingOrganization, SalesOrganization, StorageLocation


@admin.register(CompanyCode)
class CompanyCodeAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "country", "currency", "chart_of_accounts", "fiscal_year_start_month")


@admin.register(Plant)
class PlantAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "company_code")


@admin.register(StorageLocation)
class StorageLocationAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("plant", "code", "name")


@admin.register(SalesOrganization, PurchasingOrganization)
class OrgUnitAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "company_code")
