"""Demand forecasts (Wave 8a): one numbered run per plant and history, with a forecast per material.

    ForecastRun FR00000001  plant 1000, forecast from 2026-12 for 6 months, 24 months of history
      ForecastItem          TG-1001: method holt_damped, backtest MAE 1.9, MASE 0.71
        ForecastPeriod      2026-12: 52.901 (80 % interval 50.268 - 55.534), 2027-01: ...

A run is never changed after it is stored: a newer history makes a new run, and planning uses the
latest. ``fingerprint`` is the SHA-256 of exactly what was sent to the service, so asking again with
the same history returns the run already stored instead of a second one (ADR 0010).
"""
from django.db import models
from django.db.models import Q

from apps.common.models import AuditedModel


class ForecastRun(AuditedModel):
    """One call to the forecasting service for a plant: what was asked and when."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="forecast_runs")
    run_number = models.CharField(max_length=12)
    first_period = models.DateField(help_text="First month forecast (the first day of that month).")
    horizon = models.PositiveSmallIntegerField(help_text="Months forecast.")
    history_months = models.PositiveSmallIntegerField(help_text="Months of history sent, ending before first_period.")
    model_version = models.CharField(max_length=20, help_text="ml_service version that answered.")
    fingerprint = models.CharField(max_length=64, help_text="SHA-256 of the request sent to ml_service.")

    class Meta:
        ordering = ["plant__code", "-first_period", "-run_number"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "run_number"], name="unique_forecast_run_number"),
            models.UniqueConstraint(fields=["plant", "fingerprint"], name="one_run_per_history"),
        ]

    def __str__(self):
        return f"{self.company_code.code}/{self.run_number} plant {self.plant.code} from {self.first_period:%Y-%m}"


class ForecastItem(models.Model):
    """One material in a run: the method the service chose and how well it did in the backtest."""

    run = models.ForeignKey(ForecastRun, on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    method = models.CharField(max_length=20)
    mae = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True,
                              help_text="Mean absolute error in the backtest (units per month).")
    mase = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True,
                               help_text="MAE / naive method's error: below 1 beats 'same as last month'.")
    history_total = models.DecimalField(max_digits=18, decimal_places=3, help_text="Demand in the history sent.")

    class Meta:
        ordering = ["run", "material__number"]
        constraints = [models.UniqueConstraint(fields=["run", "material"], name="one_item_per_material")]


class ForecastPeriod(models.Model):
    """One month ahead for one material: the forecast quantity and its 80 % interval."""

    item = models.ForeignKey(ForecastItem, on_delete=models.CASCADE, related_name="periods")
    period = models.DateField(help_text="First day of the month.")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    lower = models.DecimalField(max_digits=18, decimal_places=3)
    upper = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        ordering = ["item", "period"]
        constraints = [
            models.UniqueConstraint(fields=["item", "period"], name="one_forecast_per_month"),
            models.CheckConstraint(condition=Q(lower__lte=models.F("quantity")) & Q(quantity__lte=models.F("upper"))
                                   & Q(lower__gte=0), name="forecast_inside_interval"),
        ]
