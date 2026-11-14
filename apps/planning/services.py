"""Planning business logic (Wave 8a): monthly demand history and forecast runs.

**Demand** is what customers took: posted goods issues for deliveries (movement 601) at the plant,
per material and calendar month. A reversed delivery cancels out in the month of the delivery it
reverses, so a correction never makes a month negative. Issues to production orders (261) are
*dependent* demand, which MRP derives from the bills of materials (Wave 8b); they are not forecast.

**A forecast run** sends every material's history to ``ml_service`` (contract ``demand-forecast/v1``,
ADR 0014) and stores the answer, numbered FR per company code:

* The months are fixed by ``first_period`` (the first month to forecast) and ``history_months``:
  the history is the months just before ``first_period``, months without demand included as zero.
* **Idempotent:** the request is fingerprinted (SHA-256). If a run with the same plant and the same
  fingerprint exists, it is returned and the service is not called; asking twice never makes two
  runs. A new delivery changes the history, so the next request makes a new run.
* The answer is checked against the request (schema, keys, months) before anything is stored, and
  stored in one transaction: a run is complete or absent.
* Runs are never changed afterwards; planning reads the latest one per material.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce, TruncMonth

from apps.finance import services as finance
from apps.finance.services import PostingError
from apps.inventory.models import MaterialDocumentItem, MovementType
from apps.masterdata.models import Material

from . import ml_client
from .models import ForecastItem, ForecastPeriod, ForecastRun

logger = logging.getLogger(__name__)

REQUEST_SCHEMA = "demand-forecast-request/v1"
RESPONSE_SCHEMA = "demand-forecast/v1"
THOUSANDTH = Decimal("0.001")
ZERO = Decimal("0.000")
MAX_HORIZON = 24
HISTORY_RANGE = (3, 120)  # months of history: at least a quarter, at most ten years
KEY_CHARACTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-")


class ForecastError(PostingError):
    """The forecast run breaks a planning rule and was not made (a 400 in the API)."""


class BadAnswer(Exception):
    """ml_service answered, but not with what the contract promises for this request (a 502)."""


@dataclass(frozen=True)
class History:
    """One material's demand per month, oldest first, and the month it starts."""

    material: Material
    start: date
    quantities: list[Decimal]

    @property
    def total(self) -> Decimal:
        """All demand in the history."""
        return sum(self.quantities, ZERO)


def month_start(day: date) -> date:
    """The first day of ``day``'s month."""
    return day.replace(day=1)


def add_months(month: date, count: int) -> date:
    """The first day of the month ``count`` months after ``month`` (negative goes back)."""
    total = month.year * 12 + month.month - 1 + count
    return date(total // 12, total % 12 + 1, 1)


def demand_history(plant, first_period: date, months: int, materials=None) -> list[History]:
    """Monthly delivered quantities per material at ``plant`` for the ``months`` before ``first_period``.

    ``materials`` limits the result to those materials (each appears even without demand); otherwise
    every material with demand in the window is included, ordered by material number.
    """
    first_period = month_start(first_period)
    start = add_months(first_period, -months)
    items = MaterialDocumentItem.objects.filter(document__movement_type=MovementType.GOODS_ISSUE_DELIVERY,
                                                plant=plant)
    # A reversal counts in the month of the delivery it reverses, so the two cancel out there.
    items = items.annotate(day=Coalesce("document__reversal_of__posting_date", "document__posting_date"))
    items = items.filter(day__gte=start, day__lt=first_period)
    if materials is not None:
        items = items.filter(material__in=materials)
    rows = (items.annotate(month=TruncMonth("day")).values("material", "month")
            .annotate(issued=Sum("quantity")).order_by())
    by_material: dict[int, dict[date, Decimal]] = {}
    for row in rows:
        month = row["month"].date() if hasattr(row["month"], "date") else row["month"]
        # Issues are negative stock changes: demand is the opposite sign.
        # A database SUM may lose the scale ("9" not "9.000"): quantities go back to three places.
        by_material.setdefault(row["material"], {})[month] = (-row["issued"]).quantize(THOUSANDTH)
    chosen = (list(materials) if materials is not None
              else list(Material.objects.filter(pk__in=by_material).order_by("number")))
    months_list = [add_months(start, i) for i in range(months)]
    return [History(material=m, start=start,
                    quantities=[max(by_material.get(m.pk, {}).get(month, ZERO), ZERO)
                                for month in months_list])
            for m in sorted(chosen, key=lambda m: m.number)]


def build_request(histories: list[History], horizon: int) -> dict:
    """The ``demand-forecast-request/v1`` for these histories (the same input, the same dict)."""
    return {"schema": REQUEST_SCHEMA, "horizon": horizon, "season_length": 12,
            "series": [{"key": h.material.number, "start": f"{h.start:%Y-%m}",
                        "history": [float(q) for q in h.quantities]} for h in histories]}


def fingerprint(request: dict) -> str:
    """SHA-256 of the request in a canonical form (sorted keys, no spaces)."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _check_answer(answer: dict, request: dict, first_period: date) -> None:
    """Refuse an answer that does not match the request: wrong schema, keys, order or months."""
    if not isinstance(answer, dict) or answer.get("schema") != RESPONSE_SCHEMA:
        schema = answer.get("schema") if isinstance(answer, dict) else None
        raise BadAnswer(f"ml_service answered with schema {schema!r}, expected {RESPONSE_SCHEMA}.")
    keys = [s.get("key") for s in answer.get("series", [])]
    if keys != [s["key"] for s in request["series"]]:
        raise BadAnswer("ml_service answered for other materials than were asked for.")
    months = [f"{add_months(first_period, i):%Y-%m}" for i in range(request["horizon"])]
    for series in answer["series"]:
        if [p.get("period") for p in series.get("forecast", [])] != months:
            raise BadAnswer(f"ml_service answered other months for {series['key']} than {months[0]}..{months[-1]}.")


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(THOUSANDTH, rounding=ROUND_HALF_UP)


def run_forecast(plant, *, first_period: date, horizon: int = 6, history_months: int = 24, materials=None,
                 user=None) -> tuple[ForecastRun, bool]:
    """Forecast demand at ``plant`` from ``first_period`` for ``horizon`` months.

    Returns ``(run, created)``: ``created`` is False when the same history was forecast before and that
    run is returned (no new number, no call to the service).
    """
    if not 1 <= horizon <= MAX_HORIZON:
        raise ForecastError(f"The horizon must be 1 to {MAX_HORIZON} months.")
    if not HISTORY_RANGE[0] <= history_months <= HISTORY_RANGE[1]:
        raise ForecastError(f"Use {HISTORY_RANGE[0]} to {HISTORY_RANGE[1]} months of history.")
    first_period = month_start(first_period)
    if materials is not None:
        materials = list(materials)
        if not materials:
            raise ForecastError("Name at least one material, or leave materials out to forecast all with demand.")
        services_ = [m.number for m in materials if not m.is_stock_managed()]
        if services_:
            raise ForecastError(f"{', '.join(services_)} are services: only stock materials are forecast.")
    histories = demand_history(plant, first_period, history_months, materials)
    if not histories:
        raise ForecastError(f"Plant {plant.code} has no delivered demand in the {history_months} months before "
                            f"{first_period:%Y-%m}: nothing to forecast.")
    odd = [h.material.number for h in histories if not set(h.material.number) <= KEY_CHARACTERS]
    if odd:
        raise ForecastError(f"Material numbers {', '.join(odd)} contain characters the forecasting contract does "
                            f"not allow.")

    request = build_request(histories, horizon)
    digest = fingerprint(request)
    existing = ForecastRun.objects.filter(plant=plant, fingerprint=digest).first()
    if existing:
        logger.info("Forecast run %s already covers this history for plant %s: returned unchanged",
                    existing.run_number, plant.code)
        return existing, False

    # The network call happens outside any database transaction, so no lock is held while waiting.
    answer = ml_client.forecast(request)
    _check_answer(answer, request, first_period)
    try:
        run = _store(plant, first_period, horizon, history_months, histories, answer, digest, user)
    except IntegrityError:
        # Another request stored the same history in the meantime: its run is the answer.
        return ForecastRun.objects.get(plant=plant, fingerprint=digest), False
    methods: dict[str, int] = {}
    for item in run.items.all():
        methods[item.method] = methods.get(item.method, 0) + 1
    logger.info("Forecast run %s for plant %s: %d material(s), %d month(s) from %s, model %s, methods %s",
                run.run_number, plant.code, len(histories), horizon, f"{first_period:%Y-%m}", run.model_version,
                dict(sorted(methods.items())))
    return run, True


@transaction.atomic
def _store(plant, first_period, horizon, history_months, histories, answer, digest, user) -> ForecastRun:
    company = plant.company_code
    run = ForecastRun.objects.create(
        company_code=company, plant=plant, run_number=finance.next_document_number(company, 0, "FR"),
        first_period=first_period, horizon=horizon, history_months=history_months,
        model_version=str(answer.get("model_version", ""))[:20], fingerprint=digest, created_by=user,
        updated_by=user)
    periods = []
    for history, series in zip(histories, answer["series"]):
        backtest = series.get("backtest") or {}
        item = ForecastItem.objects.create(run=run, material=history.material, method=str(series["method"])[:20],
                                           mae=_decimal(backtest.get("mae")), mase=_decimal(backtest.get("mase")),
                                           history_total=history.total)
        for index, point in enumerate(series["forecast"]):
            quantity = _decimal(point["quantity"])
            # Rounding can never push the interval past the forecast: clamp to keep lower <= q <= upper.
            lower = min(max(_decimal(point["lower"]), Decimal("0")), quantity)
            upper = max(_decimal(point["upper"]), quantity)
            periods.append(ForecastPeriod(item=item, period=add_months(first_period, index), quantity=quantity,
                                          lower=lower, upper=upper))
    ForecastPeriod.objects.bulk_create(periods)
    return run


def latest_forecast(plant, material) -> ForecastItem | None:
    """The material's forecast from the newest run at the plant that includes it (None if never forecast)."""
    return (ForecastItem.objects.filter(run__plant=plant, material=material)
            .select_related("run").prefetch_related("periods")
            .order_by("-run__first_period", "-run__created_at", "-run__id").first())


def forecast_all_plants(first_period: date, horizon: int = 6, history_months: int = 24) -> dict:
    """Forecast every plant that has delivered demand (the monthly Celery task). Returns counts."""
    from apps.organization.models import Plant

    result = {"created": 0, "unchanged": 0, "skipped": 0}
    for plant in Plant.objects.select_related("company_code").order_by("code"):
        try:
            _, created = run_forecast(plant, first_period=first_period, horizon=horizon,
                                      history_months=history_months)
        except ForecastError as exc:
            logger.info("Plant %s not forecast: %s", plant.code, exc)
            result["skipped"] += 1
            continue
        result["created" if created else "unchanged"] += 1
    return result
