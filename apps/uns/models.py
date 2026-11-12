"""How far the UNS publisher has read the outbox.

The outbox (``graph.OutboxEvent``) is shared with the graph projector, which marks events processed
for itself. The publisher keeps its own position instead, so the two consumers never interfere: one
row per consumer, holding the id of the last event it handled.
"""
from django.db import models


class OutboxCursor(models.Model):
    """The last outbox event a consumer has handled (``name`` = the consumer, e.g. "uns")."""

    name = models.CharField(max_length=30, unique=True)
    last_event_id = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} at outbox event {self.last_event_id}"
