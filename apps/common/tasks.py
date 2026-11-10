"""Celery task that purges expired idempotency keys (scheduled daily by Celery beat, ADR 0010)."""
from celery import shared_task

from .idempotency import purge


@shared_task(name="common.purge_idempotency_keys")
def purge_idempotency_keys():
    """Remove ``Idempotency-Key`` records older than ``IDEMPOTENCY_TTL_DAYS``."""
    return {"deleted": purge()}
