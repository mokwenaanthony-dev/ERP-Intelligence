"""Celery task that publishes new outbox events to the Unified Namespace (every 5 seconds)."""
from celery import shared_task

from .publisher import publish_outbox


@shared_task(name="uns.publish_outbox")
def publish_outbox_task():
    """Publish pending outbox events; does nothing while ``MQTT_URL`` is not set."""
    return publish_outbox()
