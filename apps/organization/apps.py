"""App configuration for company codes, plants, storage locations, sales and purchasing organisations.
"""
from django.apps import AppConfig


class OrganizationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.organization"
    verbose_name = "Organisation structure"
