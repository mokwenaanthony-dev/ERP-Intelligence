"""Celery task: forecast every plant once a month (Celery beat, the 1st at 02:15)."""
from celery import shared_task
from django.utils import timezone

from . import ml_client, services


@shared_task(name="planning.forecast_all_plants")
def forecast_all_plants():
    """Forecast from the current month for every plant with demand; does nothing while forecasting is off.

    Safe to run again: an unchanged history returns its existing run (``services.run_forecast``).
    """
    if not ml_client.enabled():
        return "forecasting is off"
    return services.forecast_all_plants(services.month_start(timezone.localdate()))
