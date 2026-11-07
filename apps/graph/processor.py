"""Projecting outbox events into the graph.

Each run takes a batch of pending events, projects every changed record once (several events for
the same record collapse into one), sends the batch to Neo4j in one transaction and marks the
events processed. If Neo4j cannot be reached the events stay pending and are retried on the next
run; a record whose projection keeps failing is marked failed after ``MAX_ATTEMPTS``.
"""
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import projection
from .models import OutboxEvent
from .store import get_store

MAX_ATTEMPTS = 5


def process_outbox(limit=500, store=None) -> dict:
    if not settings.GRAPH_ENABLED:
        return {"enabled": False, "processed": 0, "failed": 0}
    store = store or get_store()
    with transaction.atomic():
        events = list(OutboxEvent.objects.select_for_update(skip_locked=True)
                      .filter(processed_at__isnull=True, failed=False).order_by("id")[:limit])
        if not events:
            return {"enabled": True, "processed": 0, "failed": 0}
        latest = {}  # one projection per record; a deletion wins because it comes last
        for event in events:
            latest[(event.aggregate_type, event.aggregate_id)] = event.payload
        failures = {}
        for (aggregate_type, aggregate_id), payload in latest.items():
            try:
                projection.project(store, aggregate_type, aggregate_id, payload)
            except Exception as exc:  # one bad record must not block the rest
                failures[(aggregate_type, aggregate_id)] = f"{type(exc).__name__}: {exc}"
        try:
            store.flush()
        except Exception as exc:  # Neo4j unreachable: leave everything pending for the next run
            OutboxEvent.objects.filter(pk__in=[e.pk for e in events]).update(last_error=f"flush: {exc}"[:2000])
            return {"enabled": True, "processed": 0, "failed": 0, "error": str(exc)}

        now = timezone.now()
        done, failed = [], 0
        for event in events:
            error = failures.get((event.aggregate_type, event.aggregate_id))
            if error is None:
                done.append(event.pk)
                continue
            event.attempts += 1
            event.last_error = error[:2000]
            event.failed = event.attempts >= MAX_ATTEMPTS
            event.save(update_fields=["attempts", "last_error", "failed"])
            failed += 1
        OutboxEvent.objects.filter(pk__in=done).update(processed_at=now, last_error="")
        return {"enabled": True, "processed": len(done), "failed": failed}


def rebuild(store=None, clear=False) -> int:
    """Rebuild the whole graph from the ERP tables and mark pending events as handled."""
    store = store or get_store()
    store.ensure_schema(projection.ALL_LABELS)
    if clear:
        store.clear()
    started = timezone.now()
    count = projection.rebuild(store)
    OutboxEvent.objects.filter(processed_at__isnull=True, created_at__lte=started).update(processed_at=timezone.now())
    return count


def status() -> dict:
    pending = OutboxEvent.objects.filter(processed_at__isnull=True, failed=False)
    last = OutboxEvent.objects.filter(processed_at__isnull=False).order_by("-processed_at").first()
    return {
        "enabled": settings.GRAPH_ENABLED,
        "pending_events": pending.count(),
        "failed_events": OutboxEvent.objects.filter(failed=True).count(),
        "oldest_pending": pending.order_by("id").values_list("created_at", flat=True).first(),
        "last_processed": last.processed_at if last else None,
    }
