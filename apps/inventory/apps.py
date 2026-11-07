"""App configuration for goods movements and moving-average stock valuation.
"""
from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
