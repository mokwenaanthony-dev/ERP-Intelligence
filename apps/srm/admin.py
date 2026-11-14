"""Django admin screens for SRM: read-only views of the records.

Every change goes through the API, so the rules in ``services.py`` always run (an application is
approved there, a contract is released there).
"""
from django.contrib import admin

from .models import (
    Contract,
    ContractItem,
    RequestForQuotation,
    RFQItem,
    SupplierApplication,
    SupplierEvaluation,
    SupplierQuote,
)


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SupplierApplication)
class SupplierApplicationAdmin(_ReadOnly):
    list_display = ("application_number", "company_name", "bbbee_level", "status", "business_partner")
    list_filter = ("status", "bbbee_level")
    search_fields = ("application_number", "company_name", "vat_number")


class RFQItemInline(admin.TabularInline):
    model = RFQItem
    extra = 0


class SupplierQuoteInline(admin.TabularInline):
    model = SupplierQuote
    fields = ("supplier", "status", "delivery_days", "submitted_on")
    extra = 0


@admin.register(RequestForQuotation)
class RequestForQuotationAdmin(_ReadOnly):
    list_display = ("rfq_number", "title", "deadline", "status", "purchase_order")
    list_filter = ("status",)
    inlines = [RFQItemInline, SupplierQuoteInline]


class ContractItemInline(admin.TabularInline):
    model = ContractItem
    extra = 0


@admin.register(Contract)
class ContractAdmin(_ReadOnly):
    list_display = ("contract_number", "supplier", "valid_from", "valid_to", "status")
    list_filter = ("status",)
    inlines = [ContractItemInline]


@admin.register(SupplierEvaluation)
class SupplierEvaluationAdmin(_ReadOnly):
    list_display = ("supplier", "period_start", "period_end", "overall_score", "grade")
    list_filter = ("grade",)
