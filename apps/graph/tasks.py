from celery import shared_task

from . import processor


@shared_task(name="graph.process_outbox")
def process_outbox():
    """Scheduled by Celery beat every 10 seconds (``CELERY_BEAT_SCHEDULE``)."""
    return processor.process_outbox()
