"""Writing to the outbox: every change to a projected record leaves an event in the same transaction.

Saves and deletes are caught by Django signals. Services that change a status with a bulk
``.update()`` (which sends no signal) call ``emit`` themselves, e.g. when a delivery is reversed.
Nothing is written while the graph is switched off (``NEO4J_URI`` not set).
"""
from django.conf import settings
from django.db.models.signals import post_delete, post_save

from .models import OutboxEvent
from .projection import PROJECTORS, model_key


def emit(instance, payload=None):
    if not settings.GRAPH_ENABLED:
        return None
    return OutboxEvent.objects.create(aggregate_type=model_key(instance), aggregate_id=instance.pk,
                                      payload=payload or {})


def _saved(sender, instance, raw=False, **kwargs):
    if not raw:  # fixtures loading raw data are not business events
        emit(instance)


def _deleted(sender, instance, **kwargs):
    if not settings.GRAPH_ENABLED:
        return
    _, node_of = PROJECTORS[model_key(instance)]
    if node_of is None:  # e.g. a customer role: the partner's own projection is refreshed instead
        owner = getattr(instance, "business_partner", None) or getattr(instance, "plant", None)
        if owner is not None:
            emit(owner)
        return
    label, key = node_of(instance)
    emit(instance, {"deleted": True, "label": label, "key": key})


def connect_signals():
    from django.apps import apps

    for aggregate_type in PROJECTORS:
        model = apps.get_model(aggregate_type)
        post_save.connect(_saved, sender=model, dispatch_uid=f"graph-save-{aggregate_type}")
        post_delete.connect(_deleted, sender=model, dispatch_uid=f"graph-delete-{aggregate_type}")
