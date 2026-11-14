"""App configuration for supplier relationship management (Wave 7b)."""
from django.apps import AppConfig


class SrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.srm"
    verbose_name = "Supplier relationship management"
