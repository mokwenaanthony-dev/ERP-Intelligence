"""``manage.py run_mrp --plant 1000 [--date 2026-11-14] [--horizon 6]``: plan a plant and print the result.

Running it again on unchanged data prints the same run (no new number). Planned orders are only
proposals: convert them through the API (``POST /api/planning/planned-orders/<id>/convert/``).
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.common.logcontext import new_request_id, request_id
from apps.finance.services import PostingError
from apps.organization.models import Plant
from apps.planning import mrp


class Command(BaseCommand):
    help = "Run MRP for a plant (idempotent for unchanged data)."

    def add_arguments(self, parser):
        parser.add_argument("--plant", required=True)
        parser.add_argument("--date", help="Planning date, YYYY-MM-DD (default: today).")
        parser.add_argument("--horizon", type=int, default=6, help="Months to plan.")

    def handle(self, *args, plant, date=None, horizon=6, **options):
        request_id.set(f"cmd-{new_request_id()}")
        try:
            plant_obj = Plant.objects.select_related("company_code").get(code=plant)
        except Plant.DoesNotExist as exc:
            raise CommandError(f"Plant {plant} does not exist.") from exc
        try:
            planning_date = _date(date) if date else timezone.localdate()
            run, created = mrp.run_mrp(plant_obj, planning_date=planning_date, horizon=horizon)
        except (PostingError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Unchanged'} MRP run {run.run_number}: plant {plant} on {run.planning_date}, "
            f"{run.horizon} month(s)"))
        for order in run.planned_orders.select_related("material"):
            late = "  LATE" if order.is_late else ""
            self.stdout.write(f"  {order.number:>3} {order.procurement:<4} {order.material.number:<8} "
                              f"{order.quantity:>10}  start {order.start_date}  due {order.due_date}{late}")
        for message in run.messages.select_related("material"):
            self.stdout.write(self.style.WARNING(f"  {message.material.number}: {message.text}"))


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CommandError("--date must be YYYY-MM-DD.") from exc
