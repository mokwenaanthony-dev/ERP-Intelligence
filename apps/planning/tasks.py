"""Celery tasks: forecast every plant monthly (the 1st at 02:15) and run MRP daily (02:45)."""
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


@shared_task(name="planning.mrp_all_plants")
def mrp_all_plants():
    """Run MRP for every plant with something to plan (Celery beat, daily at 02:45).

    Unchanged data returns the existing run, so a quiet day adds nothing; a plant with nothing to
    plan is skipped.
    """
    from apps.organization.models import Plant

    from . import mrp

    result = {"created": 0, "unchanged": 0, "skipped": 0}
    for plant in Plant.objects.select_related("company_code").order_by("code"):
        try:
            _, created = mrp.run_mrp(plant, planning_date=timezone.localdate())
        except mrp.MRPError:
            result["skipped"] += 1
            continue
        result["created" if created else "unchanged"] += 1
    return result
