"""The transactional outbox.

Business changes write an ``OutboxEvent`` in the same database transaction as the change itself
(see ``outbox.py``), so the event exists if and only if the change was committed. A Celery task
(``tasks.process_outbox``) then projects the current state of each changed record into Neo4j.
"""
from django.db import models


class OutboxEvent(models.Model):
    """A change to project into the graph, written in the same transaction as the change itself."""

    aggregate_type = models.CharField(max_length=60, help_text="app_label.model, e.g. sales.invoice")
    aggregate_id = models.BigIntegerField()
    payload = models.JSONField(default=dict, blank=True, help_text="Only used for deletions (label and key).")
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    failed = models.BooleanField(default=False, help_text="Gave up after too many attempts; see last_error.")
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["processed_at", "failed"], name="outbox_pending_idx")]

    def __str__(self):
        state = "processed" if self.processed_at else ("failed" if self.failed else "pending")
        return f"#{self.pk} {self.aggregate_type}:{self.aggregate_id} ({state})"
