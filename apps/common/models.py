"""Abstract base models shared by every ERP module.

Every business record inherits ``AuditedModel``: timestamps plus the user who
created and last changed it. The API views (``AuditedViewMixin``) and the admin
(``AuditedAdminMixin``) fill in the user fields automatically.
"""
from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding ``created_at`` and ``updated_at``."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditedModel(TimeStampedModel):
    """Abstract base adding who created and last changed the record (ISO 27001 traceability)."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_updated",
    )

    class Meta:
        abstract = True
