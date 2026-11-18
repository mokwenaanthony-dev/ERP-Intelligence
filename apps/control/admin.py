"""Admin screens of the control tower (Wave 9b): the decision log (read-only) and the assistant switch."""
from django.contrib import admin
from django.utils import timezone

from .models import AssistantAnswer, AssistantSetting, Decision


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = ("key", "outcome", "area", "decided_by", "decided_at")
    list_filter = ("outcome", "area", "kind")
    search_fields = ("key", "title")

    def has_change_permission(self, request, obj=None):
        return False  # a decision is not rewritten afterwards (ADR 0018)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AssistantSetting)
class AssistantSettingAdmin(admin.ModelAdmin):
    list_display = ("__str__", "changed_by", "changed_at")
    readonly_fields = ("changed_by", "changed_at")

    def save_model(self, request, obj, form, change):
        obj.pk, obj.changed_by = 1, request.user  # one row; the admin who changed it is recorded
        obj.changed_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(AssistantAnswer)
class AssistantAnswerAdmin(admin.ModelAdmin):
    list_display = ("key", "model", "grounded", "asked_by", "created_at")
    readonly_fields = [f.name for f in AssistantAnswer._meta.fields]
