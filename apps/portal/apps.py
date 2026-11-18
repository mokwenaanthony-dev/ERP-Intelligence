"""App configuration for the partner portal (Wave 10a): suppliers and customers plugged in."""
from django.apps import AppConfig


class PortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"
    verbose_name = "Partner portal"
