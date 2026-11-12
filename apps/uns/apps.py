"""App configuration for the Unified Namespace bridge."""
from django.apps import AppConfig


class UnsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.uns"
    verbose_name = "Unified Namespace bridge"
