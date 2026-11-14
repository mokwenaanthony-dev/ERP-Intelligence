"""``manage.py run_forecast --plant 1000 [--first-period 2026-12] [--horizon 6] [--history-months 24]``.

Forecasts demand at a plant through ``ml_service`` and prints the run. Running it again with the
same history prints the same run (no new number). Needs ``ML_SERVICE_URL`` and ``ML_SERVICE_TOKEN``.
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.finance.services import PostingError
from apps.organization.models import Plant
from apps.planning import ml_client, services


class Command(BaseCommand):
    help = "Forecast demand at a plant through ml_service (idempotent for the same history)."

    def add_arguments(self, parser):
        parser.add_argument("--plant", required=True)
        parser.add_argument("--first-period", help="First month to forecast, YYYY-MM (default: this month).")
        parser.add_argument("--horizon", type=int, default=6)
        parser.add_argument("--history-months", type=int, default=24)

    def handle(self, *args, plant, first_period=None, horizon=6, history_months=24, **options):
        try:
            plant_obj = Plant.objects.select_related("company_code").get(code=plant)
        except Plant.DoesNotExist as exc:
            raise CommandError(f"Plant {plant} does not exist.") from exc
        try:
            first = date.fromisoformat(f"{first_period}-01") if first_period else timezone.localdate()
        except ValueError as exc:
            raise CommandError("--first-period must be YYYY-MM.") from exc
        try:
            run, created = services.run_forecast(plant_obj, first_period=first, horizon=horizon,
                                                 history_months=history_months)
        except (PostingError, ml_client.ServiceOff, ml_client.ServiceRejected, services.BadAnswer) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Unchanged'} forecast run {run.run_number}: plant {plant}, "
            f"{run.horizon} month(s) from {run.first_period:%Y-%m}, model {run.model_version}"))
        for item in run.items.select_related("material").prefetch_related("periods"):
            months = ", ".join(f"{p.period:%Y-%m} {p.quantity}" for p in item.periods.all()[:3])
            self.stdout.write(f"  {item.material.number:<10} {item.method:<15} MAE {item.mae}  {months} ...")
