from celery import shared_task

from . import services


@shared_task(name="analytics.refresh_views")
def refresh_views():
    """Scheduled by Celery beat (``CELERY_BEAT_SCHEDULE``); returns the views refreshed."""
    return [row["view"] for row in services.refresh_views()]
