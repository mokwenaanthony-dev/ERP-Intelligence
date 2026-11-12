"""Writing to the outbox: every change to a projected record leaves an event in the same transaction.

Saves and deletes are caught by Django signals. Services that change a status with a bulk
``.update()`` (which sends no signal) call ``emit`` themselves, e.g. when a delivery is reversed.
Nothing is written while both consumers are off: the graph (``NEO4J_URI``) and the UNS bridge
(``MQTT_URL``, Wave 6), which reads the same events with its own cursor.
"""
from django.conf import settings
from django.db.models.signals import post_delete, post_save

from .models import OutboxEvent
from .projection import PROJECTORS, model_key


def emit(instance, payload=None):
    """Record that ``instance`` changed so the projector refreshes it. Call this after bulk ``.update()``
    calls, which do not send ``post_save`` signals.
    """
    if not _enabled():
        return None
    return OutboxEvent.objects.create(aggregate_type=model_key(instance), aggregate_id=instance.pk,
                                      payload=payload or {})


def _enabled():
    return settings.GRAPH_ENABLED or settings.UNS_ENABLED


def _saved(sender, instance, raw=False, **kwargs):
    if not raw:  # fixtures loading raw data are not business events
        emit(instance)


def _deleted(sender, instance, **kwargs):
    if not _enabled():
        return
    _, node_of = PROJECTORS[model_key(instance)]
    if node_of is None:  # e.g. a customer role: the partner's own projection is refreshed instead
        owner = (getattr(instance, "business_partner", None) or getattr(instance, "plant", None)
                 or getattr(instance, "work_center", None))  # a cell's state lives on its work centre's node
        if owner is not None:
            emit(owner)
        return
    label, key = node_of(instance)
    payload = {"deleted": True, "label": label, "key": key}
    if hasattr(instance, "uns_path"):  # the UNS publisher clears the retained topic of a deleted cell
        payload["uns_path"] = instance.uns_path
    emit(instance, payload)


def connect_signals():
    """Write an outbox event on every save and delete of a projected model (called from ``ready()``)."""
    from django.apps import apps

    for aggregate_type in PROJECTORS:
        model = apps.get_model(aggregate_type)
        post_save.connect(_saved, sender=model, dispatch_uid=f"graph-save-{aggregate_type}")
        post_delete.connect(_deleted, sender=model, dispatch_uid=f"graph-delete-{aggregate_type}")
