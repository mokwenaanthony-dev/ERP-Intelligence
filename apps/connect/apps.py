"""App configuration for side-by-side mode (Wave 10b): loading data from an ERP that stays in place."""
from django.apps import AppConfig


class ConnectConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.connect"
    verbose_name = "Side by side (imports from another ERP)"
