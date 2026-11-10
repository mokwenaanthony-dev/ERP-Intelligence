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


class IdempotencyRecord(models.Model):
    """One ``Idempotency-Key`` a user sent, and the response to replay if the request comes again (ADR 0010).

    ``status_code`` is empty while the first request is still running. Only successful responses are
    kept; a failed request deletes its record, because it changed nothing and may be retried.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=128)
    method = models.CharField(max_length=7)
    path = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64, help_text="SHA-256 of method, path and request body.")
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    response_body = models.TextField(blank=True)
    created_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_idempotency_key_per_user")]
        indexes = [models.Index(fields=["created_at"], name="idempotency_created_idx")]

    def __str__(self):
        return f"{self.user_id}:{self.key} {self.method} {self.path} -> {self.status_code}"
