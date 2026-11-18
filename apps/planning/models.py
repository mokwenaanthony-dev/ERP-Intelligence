"""Demand forecasts (Wave 8a) and MRP (Wave 8b).

Forecasts: one numbered run per plant and history, with a forecast per material.

    ForecastRun FR00000001  plant 1000, forecast from 2026-12 for 6 months, 24 months of history
      ForecastItem          TG-1001: method holt_damped, backtest MAE 1.9, MASE 0.71
        ForecastPeriod      2026-12: 52.901 (80 % interval 50.268 - 55.534), 2027-01: ...

A run is never changed after it is stored: a newer history makes a new run, and planning uses the
latest. ``fingerprint`` is the SHA-256 of exactly what was sent to the service, so asking again with
the same history returns the run already stored instead of a second one (ADR 0010).

MRP (``apps.planning.mrp``, ADR 0015): ``MaterialPlanning`` holds how a material is planned at a plant;
each ``MRPRun`` keeps, per material (``MRPMaterial``), a monthly requirements list (``PlanningLine``),
the ``PlannedOrder`` proposals and the ``MRPMessage`` exceptions for the planner.
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


# --- MRP (Wave 8b) -------------------------------------------------------------------------------------

class MaterialPlanning(AuditedModel):
    """How MRP plans a material at a plant (SAP MRP views): make or buy, lot size, safety stock, lead time.

    Optional: a material without a row is planned with the defaults in ``apps.planning.mrp`` (make if it
    has an active BOM at the plant, otherwise buy; lot for lot; no safety stock).
    """

    class Procurement(models.TextChoices):
        MAKE = "MAKE", "In-house production"
        BUY = "BUY", "External procurement"

    class LotSize(models.TextChoices):
        LOT_FOR_LOT = "EX", "Lot for lot (exactly the shortage)"
        FIXED = "FX", "Multiples of a fixed lot"
        MINIMUM = "MN", "At least a minimum lot"

    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    procurement = models.CharField(max_length=4, choices=Procurement.choices)
    lot_size = models.CharField(max_length=2, choices=LotSize.choices, default=LotSize.LOT_FOR_LOT)
    lot_quantity = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True,
                                       help_text="The fixed or minimum lot (FX and MN only).")
    safety_stock = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    lead_time_days = models.PositiveSmallIntegerField(help_text="Days from start (order) to availability.")
    preferred_supplier = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, null=True,
                                           blank=True, related_name="+",
                                           help_text="Buy from this supplier when no contract covers the material.")

    class Meta:
        ordering = ["plant__code", "material__number"]
        constraints = [
            models.UniqueConstraint(fields=["material", "plant"], name="one_planning_view_per_plant"),
            models.CheckConstraint(condition=Q(safety_stock__gte=0), name="safety_stock_non_negative"),
            models.CheckConstraint(condition=Q(lot_size="EX") | Q(lot_quantity__gt=0),
                                   name="fixed_and_minimum_lots_need_a_quantity"),
        ]

    def __str__(self):
        return f"{self.material.number} @ {self.plant.code}: {self.procurement} {self.lot_size}"


class MRPRun(AuditedModel):
    """One MRP run for a plant on a planning date: the requirements list and the planned orders.

    ``fingerprint`` is the SHA-256 of everything the run read (parameters, stock, demand, receipts,
    BOMs and the date): running MRP again on unchanged data returns this run instead of a new one.
    """

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="mrp_runs")
    run_number = models.CharField(max_length=12)
    planning_date = models.DateField()
    first_period = models.DateField(help_text="First month planned (the month of the planning date).")
    horizon = models.PositiveSmallIntegerField(help_text="Months planned.")
    fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["plant__code", "-planning_date", "-run_number"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "run_number"], name="unique_mrp_run_number"),
            models.UniqueConstraint(fields=["plant", "fingerprint"], name="one_mrp_run_per_situation"),
        ]

    def __str__(self):
        return f"{self.company_code.code}/{self.run_number} plant {self.plant.code} on {self.planning_date}"


class MRPMaterial(models.Model):
    """A material in an MRP run: how it was planned and the stock it started from."""

    run = models.ForeignKey(MRPRun, on_delete=models.CASCADE, related_name="materials")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    low_level_code = models.PositiveSmallIntegerField(help_text="0 = sold, 1 = used in level 0, and so on.")
    procurement = models.CharField(max_length=4, choices=MaterialPlanning.Procurement.choices)
    opening_stock = models.DecimalField(max_digits=18, decimal_places=3)
    safety_stock = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        ordering = ["run", "low_level_code", "material__number"]
        constraints = [models.UniqueConstraint(fields=["run", "material"], name="one_mrp_line_per_material")]


class PlanningLine(models.Model):
    """One month of a material's requirements list (SAP MD04): what comes in, goes out and is left."""

    item = models.ForeignKey(MRPMaterial, on_delete=models.CASCADE, related_name="lines")
    period = models.DateField(help_text="First day of the month.")
    independent = models.DecimalField(max_digits=18, decimal_places=3, help_text="Forecast or sales orders.")
    dependent = models.DecimalField(max_digits=18, decimal_places=3, help_text="Components for production.")
    receipts = models.DecimalField(max_digits=18, decimal_places=3, help_text="Open purchase and production orders.")
    planned = models.DecimalField(max_digits=18, decimal_places=3, help_text="Planned orders due this month.")
    available = models.DecimalField(max_digits=18, decimal_places=3, help_text="Projected stock at month end.")

    class Meta:
        ordering = ["item", "period"]
        constraints = [models.UniqueConstraint(fields=["item", "period"], name="one_planning_line_per_month")]


class PlannedOrder(models.Model):
    """A proposal from MRP to make or buy a quantity by a date; converted into a real order by a person.

    OPEN until converted (CONVERTED, with the production or purchase order number) or replaced by a
    later run (SUPERSEDED). Converting twice returns the same document.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CONVERTED = "CONVERTED", "Converted"
        SUPERSEDED = "SUPERSEDED", "Superseded by a later run"

    run = models.ForeignKey(MRPRun, on_delete=models.CASCADE, related_name="planned_orders")
    number = models.PositiveIntegerField(help_text="Sequence within the run.")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    procurement = models.CharField(max_length=4, choices=MaterialPlanning.Procurement.choices)
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    start_date = models.DateField(help_text="When production must start or the order must be placed.")
    due_date = models.DateField(help_text="When the quantity must be available.")
    is_late = models.BooleanField(default=False, help_text="The start date is before the planning date.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    document_number = models.CharField(max_length=20, blank=True, help_text="Production or purchase order.")

    class Meta:
        ordering = ["run", "number"]
        constraints = [
            models.UniqueConstraint(fields=["run", "number"], name="unique_planned_order_number"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="planned_quantity_positive"),
        ]

    def __str__(self):
        return f"{self.run.run_number}/{self.number} {self.procurement} {self.material.number} x {self.quantity}"


class MRPMessage(models.Model):
    """Something the planner must look at: an order to start in the past, a missing BOM or supplier."""

    run = models.ForeignKey(MRPRun, on_delete=models.CASCADE, related_name="messages")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    text = models.CharField(max_length=200)
    planned_order = models.ForeignKey(PlannedOrder, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="messages",
                                      help_text="The late planned order the message is about (v0.20.1), if any.")

    class Meta:
        ordering = ["run", "material__number", "id"]
