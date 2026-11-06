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
