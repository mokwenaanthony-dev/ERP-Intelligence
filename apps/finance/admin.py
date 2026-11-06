from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import (
    ChartOfAccounts,
    FiscalYear,
    GLAccount,
    JournalEntry,
    JournalLine,
    OpenItemClearing,
    PostingPeriod,
    TaxCode,
)


@admin.register(ChartOfAccounts)
class ChartOfAccountsAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name")


@admin.register(GLAccount)
class GLAccountAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "name", "chart_of_accounts", "account_type", "reconciliation_type", "is_posting_blocked")
    list_filter = ("chart_of_accounts", "account_type", "reconciliation_type")
    search_fields = ("number", "name")


@admin.register(TaxCode)
class TaxCodeAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "description", "chart_of_accounts", "tax_type", "rate", "gl_account")


class PostingPeriodInline(admin.TabularInline):
    model = PostingPeriod
    extra = 0
    fields = ("number", "start_date", "end_date", "status")
    readonly_fields = ("number", "start_date", "end_date")
    can_delete = False


@admin.register(FiscalYear)
class FiscalYearAdmin(AuditedAdminMixin, admin.ModelAdmin):
    """Create fiscal years through the API so their periods are generated; here you can open/close periods."""

    list_display = ("company_code", "year", "start_date", "end_date")
    readonly_fields = AuditedAdminMixin.readonly_fields + ("company_code", "year", "start_date", "end_date")
    inlines = [PostingPeriodInline]

    def has_add_permission(self, request):
        return False


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    can_delete = False
    fields = ("line_number", "gl_account", "debit_credit", "amount", "currency", "amount_local",
              "business_partner", "tax_code", "text")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    """Read-only: entries are posted through the API and corrected by reversal."""

    list_display = ("document_number", "company_code", "fiscal_year", "period", "document_type", "posting_date",
                    "status")
    list_filter = ("company_code", "fiscal_year", "document_type", "status")
    search_fields = ("document_number", "reference", "header_text")
    inlines = [JournalLineInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OpenItemClearing)
class OpenItemClearingAdmin(admin.ModelAdmin):
    """Read-only record of which payment cleared which open item."""

    list_display = ("clearing_entry", "item", "amount", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
