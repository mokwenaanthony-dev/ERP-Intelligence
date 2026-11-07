"""Django admin screens for HR and payroll.

Posted documents are shown read-only: the admin is for looking things up and maintaining
configuration, never for editing a posting (corrections go through reversal in the API).
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import Employee, OrgUnit, PayeBracket, PayeTable, PayrollResult, PayrollRun, PayrollSetting, Position


@admin.register(OrgUnit)
class OrgUnitAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "parent", "cost_center", "company_code")


@admin.register(Position)
class PositionAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "title", "org_unit", "cost_center")


@admin.register(Employee)
class EmployeeAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("personnel_number", "first_name", "last_name", "position", "hire_date", "termination_date")
    search_fields = ("personnel_number", "first_name", "last_name")
    readonly_fields = ("personnel_number",)

    def has_add_permission(self, request):
        return False  # hired through the API so the personnel number comes from the number range


class PayeBracketInline(admin.TabularInline):
    model = PayeBracket
    extra = 0


@admin.register(PayeTable)
class PayeTableAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("tax_year", "primary_rebate")
    inlines = [PayeBracketInline]


@admin.register(PayrollSetting)
class PayrollSettingAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("company_code", "uif_rate", "uif_monthly_ceiling", "sdl_rate")


class PayrollResultInline(admin.TabularInline):
    model = PayrollResult
    extra = 0
    can_delete = False
    fields = ("employee", "cost_center", "gross", "paye", "uif_employee", "uif_employer", "sdl", "net")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PayrollRun)
class PayrollRunAdmin(admin.ModelAdmin):
    """Payroll runs are posted and reversed through the API so the journal always agrees."""

    list_display = ("run_number", "year", "month", "gross", "net", "status", "journal_entry")
    list_filter = ("status", "year")
    inlines = [PayrollResultInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
