"""App configuration for cost centres, profit centres and their reports.
"""
from django.apps import AppConfig


class ControllingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.controlling"
    verbose_name = "Controlling"
