"""App configuration for planning (Wave 8a: demand forecasting)."""
from django.apps import AppConfig


class PlanningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.planning"
    verbose_name = "Planning"
