"""Django admin screens for cost centres, profit centres and their reports.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import CostCenter, PlantProfitCenter, ProfitCenter


@admin.register(ProfitCenter)
class ProfitCenterAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "company_code", "is_active")
    list_filter = ("company_code", "is_active")


@admin.register(CostCenter)
class CostCenterAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "category", "profit_center", "company_code", "is_active")
    list_filter = ("company_code", "category", "is_active")


@admin.register(PlantProfitCenter)
class PlantProfitCenterAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("plant", "profit_center")
