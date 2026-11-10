"""Django admin screens for production planning.

The equipment hierarchy and costing data can be maintained here. BOMs and routings are read-only:
they are created through the API so the cycle and work-centre checks in ``services.py`` always run.
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import (
    ActivityRate,
    ActivityType,
    BillOfMaterials,
    BOMItem,
    ProductionArea,
    ProductionLine,
    Routing,
    RoutingOperation,
    WorkCenter,
)


@admin.register(ProductionArea)
class ProductionAreaAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "plant", "is_active")
    list_filter = ("plant", "is_active")


@admin.register(ProductionLine)
class ProductionLineAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "area", "is_active")
    list_filter = ("area__plant", "is_active")


@admin.register(WorkCenter)
class WorkCenterAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "line", "isa95_level", "cost_center", "is_active")
    list_filter = ("plant", "isa95_level", "is_active")


@admin.register(ActivityType)
class ActivityTypeAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "unit", "allocation_account", "company_code", "is_active")


@admin.register(ActivityRate)
class ActivityRateAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("cost_center", "activity_type", "valid_from", "price")
    list_filter = ("activity_type",)


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class BOMItemInline(_ReadOnlyInline):
    model = BOMItem
    fields = ("item_number", "component", "quantity")


class RoutingOperationInline(_ReadOnlyInline):
    model = RoutingOperation
    fields = ("operation_number", "description", "work_center")


class _RecipeAdmin(admin.ModelAdmin):
    list_display = ("material", "plant", "valid_from", "base_quantity", "status")
    list_filter = ("plant", "status")
    search_fields = ("material__number",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BillOfMaterials)
class BillOfMaterialsAdmin(_RecipeAdmin):
    inlines = [BOMItemInline]


@admin.register(Routing)
class RoutingAdmin(_RecipeAdmin):
    inlines = [RoutingOperationInline]
