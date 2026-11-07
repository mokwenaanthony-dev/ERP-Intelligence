from django.apps import AppConfig


class GraphConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.graph"
    verbose_name = "Knowledge graph"

    def ready(self):
        from . import outbox

        outbox.connect_signals()
