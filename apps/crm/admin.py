"""Django admin screens for CRM.

Campaigns can be maintained here. Leads, opportunities, quotations and tickets are read-only: they
change status through the API, so the rules in ``services.py`` always run.
"""
from django.contrib import admin

from apps.common.admin import AuditedAdminMixin

from .models import Activity, Campaign, Lead, Opportunity, Quotation, QuotationItem, ServiceTicket


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "company_code", "channel", "start_date", "end_date", "budget")
    list_filter = ("company_code", "channel")


@admin.register(Lead)
class LeadAdmin(_ReadOnly):
    list_display = ("lead_number", "company_name", "source", "campaign", "status", "owner")
    list_filter = ("status", "source")
    search_fields = ("lead_number", "company_name")


@admin.register(Opportunity)
class OpportunityAdmin(_ReadOnly):
    list_display = ("opportunity_number", "title", "customer", "stage", "probability", "expected_value",
                    "expected_close_date")
    list_filter = ("stage",)
    search_fields = ("opportunity_number", "title", "customer__number")


class QuotationItemInline(admin.TabularInline):
    model = QuotationItem
    fields = ("item_number", "material", "quantity", "net_price", "price_overridden")
    extra = 0


@admin.register(Quotation)
class QuotationAdmin(_ReadOnly):
    list_display = ("quotation_number", "customer", "quotation_date", "valid_until", "status", "sales_order")
    list_filter = ("status",)
    inlines = [QuotationItemInline]


@admin.register(ServiceTicket)
class ServiceTicketAdmin(_ReadOnly):
    list_display = ("ticket_number", "subject", "customer", "priority", "status", "due_at", "resolved_at")
    list_filter = ("status", "priority")
    search_fields = ("ticket_number", "subject", "customer__number")


@admin.register(Activity)
class ActivityAdmin(_ReadOnly):
    list_display = ("kind", "subject", "due_date", "completed_on", "owner")
    list_filter = ("kind",)
