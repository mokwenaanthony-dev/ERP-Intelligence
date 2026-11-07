"""App configuration for the knowledge graph.

``ready()`` connects the model signals that write outbox events, so every save and delete of a
projected model is recorded in the same transaction as the change itself.
"""
from django.apps import AppConfig


class GraphConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.graph"
    verbose_name = "Knowledge graph"

    def ready(self):
        from . import outbox

        outbox.connect_signals()
