"""Admin helpers shared by all ERP modules."""


class AuditedAdminMixin:
    """Fills created_by / updated_by when records are saved through the Django admin."""

    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if hasattr(instance, "created_by_id") and instance.pk is None:
                instance.created_by = request.user
            if hasattr(instance, "updated_by_id"):
                instance.updated_by = request.user
            instance.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()
