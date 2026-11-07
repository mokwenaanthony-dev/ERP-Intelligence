"""App configuration for business partners, materials, currencies, units and exchange rates.
"""
from django.apps import AppConfig


class MasterdataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.masterdata"
    verbose_name = "Master data"
