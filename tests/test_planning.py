"""Planning (Wave 8a): demand history from deliveries and forecast runs through ml_service.

The service is replaced by a fake at the one network function (``ml_client.post_json``), so no test
needs it running. The fake answers like the real service (the worked example in
``ml_service/contract/`` is checked too), counts its calls, and can fail on purpose to test retries.
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apps.accounts.models import Role
from apps.inventory import services as inventory
from apps.inventory.models import MovementType
from apps.inventory.services import ItemInput
from apps.masterdata.models import Material
from apps.organization.models import Plant, StorageLocation
from apps.planning import ml_client, services
from apps.planning.models import ForecastPeriod, ForecastRun
from apps.planning.services import ForecastError

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/planning"
CONTRACT = Path(__file__).resolve().parents[1] / "ml_service" / "contract"
NOVEMBER = date(2026, 11, 1)
WIDGET_DEMAND = [5, 6, 7, 8, 9, 10, 11, 12]  # March to October 2026
GADGET_DEMAND = [0, 3, 0, 0, 4, 0, 0, 2]


class FakeService:
    """Answers forecast requests like ml_service would (naive method), and records what it was sent."""

    def __init__(self):
        self.calls, self.failures, self.status = [], 0, 200

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": json.loads(body), "headers": headers})
        if self.failures:
            self.failures -= 1
            raise ConnectionRefusedError("ml_service is starting")
        if self.status != 200:
            return self.status, json.dumps({"detail": "bad"}).encode()
        request = json.loads(body)
        return 200, json.dumps(self.answer(request)).encode()

    @staticmethod
    def answer(request):
        series = []
        for s in request["series"]:
            year, month = int(s["start"][:4]), int(s["start"][5:7])
            first = year * 12 + month - 1 + len(s["history"])
            last = s["history"][-1]
            series.append({"key": s["key"], "method": "naive",
                           "backtest": {"mae": 1.5, "mase": 0.8, "points": 6, "candidates": {"naive": 1.5}},
                           "forecast": [{"period": f"{(first + h) // 12:04d}-{(first + h) % 12 + 1:02d}",
                                         "quantity": last, "lower": max(last - 2, 0), "upper": last + 2.0004}
                                        for h in range(request["horizon"])]})
        return {"schema": "demand-forecast/v1", "model_version": "0.1.0", "horizon": request["horizon"],
                "series": series}


@pytest.fixture
def service(settings, monkeypatch):
    settings.ML_SERVICE_URL = "http://ml_service:8001"
    fake = FakeService()
    monkeypatch.setattr(ml_client, "post_json", fake)
    monkeypatch.setattr(ml_client.time, "sleep", lambda seconds: None)  # retries without waiting
    return fake


@pytest.fixture
def planning(demo):
    demo.plant = Plant.objects.get(code="1000")
    demo.main = StorageLocation.objects.get(plant=demo.plant, code="0001")
    demo.widget = Material.objects.get(number="TG-1001")
    demo.gadget = Material.objects.get(number="TG-1002")
    for month, (widgets, gadgets) in enumerate(zip(WIDGET_DEMAND, GADGET_DEMAND), start=3):
        items = [ItemInput(demo.widget, demo.main, D(widgets))]
        if gadgets:
            items.append(ItemInput(demo.gadget, demo.main, D(gadgets)))
        demo.last_issue = inventory.post_goods_movement(movement_type=MovementType.GOODS_ISSUE_DELIVERY,
                                                        plant=demo.plant, posting_date=date(2026, month, 10),
                                                        items=items)
    return demo


# --- demand history -----------------------------------------------------------------------------------

def test_demand_history_is_delivered_quantity_per_month(planning):
    histories = services.demand_history(planning.plant, NOVEMBER, 8)
    assert [h.material.number for h in histories] == ["TG-1001", "TG-1002"]
    widget, gadget = histories
    assert widget.start == date(2026, 3, 1) and widget.quantities == [D(q) for q in WIDGET_DEMAND]
    assert gadget.quantities == [D(q) for q in GADGET_DEMAND] and gadget.total == D("9")
    # A longer window starts earlier and fills the months before the first delivery with zero.
    assert services.demand_history(planning.plant, NOVEMBER, 10)[0].quantities[:3] == [D(0), D(0), D(5)]


def test_a_reversed_delivery_cancels_out_in_its_own_month(planning):
    inventory.reverse_material_document(planning.last_issue, posting_date=date(2026, 11, 2))
    widget = services.demand_history(planning.plant, NOVEMBER, 8)[0]
    assert widget.quantities[-1] == D("0")  # October's 12 is gone, and November is not negative
    assert services.demand_history(planning.plant, date(2026, 12, 1), 9)[0].quantities[-1] == D("0")


def test_the_request_matches_the_contract_example(planning):
    request = services.build_request(services.demand_history(planning.plant, NOVEMBER, 8), 3)
    example = json.loads((CONTRACT / "example_request.json").read_text())
    assert set(request) == set(example) and set(request["series"][0]) <= set(example["series"][0]) | {"method"}
    assert request["series"][0] == {"key": "TG-1001", "start": "2026-03", "history": [float(q) for q in WIDGET_DEMAND]}
    assert services.fingerprint(request) == services.fingerprint(json.loads(json.dumps(request)))  # stable


def test_the_contract_examples_pass_the_erp_checks():
    request = json.loads((CONTRACT / "example_request.json").read_text())
    answer = json.loads((CONTRACT / "example_response.json").read_text())
    services._check_answer(answer, request, date(2026, 1, 1))  # the month after the example history


# --- forecast runs ------------------------------------------------------------------------------------

def test_a_forecast_run_stores_the_answer(planning, service):
    run, created = services.run_forecast(planning.plant, first_period=date(2026, 11, 20), horizon=3,
                                         history_months=8)
    assert created and run.run_number == "FR00000001" and run.first_period == NOVEMBER
    assert (run.model_version, run.horizon, run.history_months) == ("0.1.0", 3, 8)
    call = service.calls[0]
    assert call["url"] == "http://ml_service:8001/v1/forecast"
    assert call["headers"]["X-Service-Token"] == "test-token-at-least-16" and call["headers"]["X-Request-ID"]
    widget = run.items.get(material=planning.widget)
    assert (widget.method, widget.mae, widget.mase, widget.history_total) == ("naive", D("1.500"), D("0.800"),
                                                                              D("68.000"))
    periods = list(widget.periods.values_list("period", "quantity", "lower", "upper"))
    assert periods[0] == (NOVEMBER, D("12.000"), D("10.000"), D("14.000"))  # 14.0004 rounded to 3 places
    assert [p[0] for p in periods] == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]
    assert services.latest_forecast(planning.plant, planning.gadget).periods.first().quantity == D("2.000")


def test_the_same_history_returns_the_same_run_without_calling_again(planning, service):
    first, created = services.run_forecast(planning.plant, first_period=NOVEMBER, horizon=3, history_months=8)
    again, created_again = services.run_forecast(planning.plant, first_period=NOVEMBER, horizon=3, history_months=8)
    assert created and not created_again and again == first and len(service.calls) == 1
    assert ForecastRun.objects.count() == 1
    # New demand changes the history: a new run, with a new number.
    inventory.post_goods_movement(movement_type=MovementType.GOODS_ISSUE_DELIVERY, plant=planning.plant,
                                  posting_date=date(2026, 10, 20), items=[ItemInput(planning.widget, planning.main,
                                                                                    D("1"))])
    newer, created_newer = services.run_forecast(planning.plant, first_period=NOVEMBER, horizon=3, history_months=8)
    assert created_newer and newer.run_number == "FR00000002"
    assert services.latest_forecast(planning.plant, planning.widget).run == newer


def test_planning_rules(planning, service):
    with pytest.raises(ForecastError, match="1 to 24 months"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, horizon=25)
    with pytest.raises(ForecastError, match="3 to 120 months"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, history_months=2)
    with pytest.raises(ForecastError, match="no delivered demand"):
        services.run_forecast(planning.plant, first_period=date(2026, 3, 1), history_months=3)
    with pytest.raises(ForecastError, match="at least one material"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, materials=[])
    assert service.calls == []  # every rule is checked before the service is called


def test_retries_and_failures(planning, service, caplog):
    service.failures = 2  # the service is still starting: two refused connections, then an answer
    run, created = services.run_forecast(planning.plant, first_period=NOVEMBER, history_months=8)
    assert created and len(service.calls) == 3
    assert "attempt 1 of 3 failed (ConnectionRefusedError)" in caplog.text
    service.failures = 3
    with pytest.raises(ml_client.ServiceOff, match="after 3 attempt"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, horizon=2, history_months=8)
    service.status = 401
    with pytest.raises(ml_client.ServiceRejected, match="ML_SERVICE_TOKEN must match"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, horizon=2, history_months=8)
    assert ForecastRun.objects.count() == 1  # nothing stored by the failed attempts
    assert "test-token-at-least-16" not in caplog.text  # the token is never logged


def test_an_answer_outside_the_contract_is_refused(planning, service, monkeypatch):
    monkeypatch.setattr(FakeService, "answer", staticmethod(lambda request: {"schema": "demand-forecast/v2"}))
    with pytest.raises(services.BadAnswer, match="schema"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, history_months=8)
    assert ForecastPeriod.objects.count() == 0


def test_forecasting_is_off_without_a_url(planning, settings):
    settings.ML_SERVICE_URL = ""
    with pytest.raises(ml_client.ServiceOff, match="Forecasting is off"):
        services.run_forecast(planning.plant, first_period=NOVEMBER, history_months=8)


def test_the_monthly_task(planning, service, settings):
    from apps.planning import tasks

    assert services.forecast_all_plants(NOVEMBER, history_months=8) == {"created": 1, "unchanged": 0, "skipped": 0}
    assert services.forecast_all_plants(NOVEMBER, history_months=8)["unchanged"] == 1  # again: same run
    settings.ML_SERVICE_URL = ""
    assert tasks.forecast_all_plants() == "forecasting is off"


# --- API ------------------------------------------------------------------------------------------------

def test_the_planner_forecasts_through_the_api(planning, service, client_for):
    planner = client_for(Role.PRODUCTION_PLANNER)
    history = planner.get(f"{API}/demand-history/?plant=1000&first_period=2026-11&months=8&material=TG-1002").json()
    assert history["materials"][0]["total"] == "9.000"
    assert history["materials"][0]["history"][1] == {"period": "2026-04", "quantity": "3.000"}

    body = {"plant": "1000", "first_period": "2026-11", "horizon": 3, "history_months": 8}
    created = planner.post(f"{API}/forecast-runs/", body, format="json")
    assert created.status_code == 201, created.data
    data = created.json()
    assert data["run_number"] == "FR00000001" and data["first_period"] == "2026-11"
    assert data["items"][0]["periods"][0] == {"period": "2026-11", "quantity": "12.000", "lower": "10.000",
                                              "upper": "14.000"}
    again = planner.post(f"{API}/forecast-runs/", body, format="json")  # a new Idempotency-Key, same history
    assert again.status_code == 200 and again.json()["run_number"] == "FR00000001"

    assert planner.get(f"{API}/forecast-runs/?plant=1000").json()["results"][0]["materials"] == 2
    latest = planner.get(f"{API}/forecast/?plant=1000&material=TG-1001").json()
    assert latest["run_number"] == "FR00000001" and latest["method"] == "naive"
    assert planner.get(f"{API}/forecast/?plant=1000").status_code == 400  # material is required


def test_api_errors_and_roles(planning, service, client_for, settings):
    planner = client_for(Role.PRODUCTION_PLANNER)
    refused = planner.post(f"{API}/forecast-runs/", {"plant": "1000", "first_period": "2026-03",
                                                     "history_months": 3}, format="json")
    assert refused.status_code == 400 and "no delivered demand" in refused.data["non_field_errors"][0]
    service.status = 422
    assert planner.post(f"{API}/forecast-runs/", {"plant": "1000", "first_period": "2026-11", "history_months": 8},
                        format="json").status_code == 502
    settings.ML_SERVICE_URL = ""
    off = planner.post(f"{API}/forecast-runs/", {"plant": "1000", "first_period": "2026-11", "history_months": 8},
                       format="json")
    assert off.status_code == 503 and "Forecasting is off" in off.data["detail"]
    assert planner.get(f"{API}/forecast/?plant=1000&material=TG-1001").status_code == 404

    assert client_for(Role.VIEWER).get(f"{API}/forecast-runs/").status_code == 200
    assert client_for(Role.SALES_REP).post(f"{API}/forecast-runs/", {}, format="json").status_code == 403
    assert client_for(Role.HR_ADMIN).get(f"{API}/forecast-runs/").status_code == 403


def test_the_command(planning, service):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("run_forecast", "--plant", "1000", "--first-period", "2026-11", "--history-months", "8",
                 "--horizon", "2", stdout=out)
    call_command("run_forecast", "--plant", "1000", "--first-period", "2026-11", "--history-months", "8",
                 "--horizon", "2", stdout=out)
    assert "Created forecast run FR00000001" in out.getvalue() and "Unchanged forecast run FR00000001" in out.getvalue()
    assert "TG-1001" in out.getvalue()
