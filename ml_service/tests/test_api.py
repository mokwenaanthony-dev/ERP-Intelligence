"""The HTTP layer: token, request IDs, validation errors, logging. Needs FastAPI and httpx (CI installs them)."""
import json
import logging
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token-at-least-16"
EXAMPLE = json.loads((Path(__file__).resolve().parents[1] / "contract" / "example_request.json").read_text())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ML_SERVICE_TOKEN", TOKEN)
    import importlib

    from app import main
    importlib.reload(main)  # read the token set above
    return TestClient(main.app)


def test_the_service_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setenv("ML_SERVICE_TOKEN", "short")
    import importlib

    from app import main
    with pytest.raises(RuntimeError, match="at least 16 characters"):
        importlib.reload(main)


def test_health_needs_no_token(client):
    answer = client.get("/health")
    assert answer.status_code == 200 and answer.json()["status"] == "ok" and answer.headers["X-Request-ID"]


def test_forecast_needs_the_token(client, caplog):
    assert client.post("/v1/forecast", json=EXAMPLE).status_code == 401
    assert client.post("/v1/forecast", json=EXAMPLE, headers={"X-Service-Token": "wrong"}).status_code == 401
    assert TOKEN not in caplog.text


def test_forecast_answers_the_contract_and_repeats_identically(client, caplog):
    caplog.set_level(logging.INFO)
    headers = {"X-Service-Token": TOKEN, "X-Request-ID": "erp-req-12345678"}
    first = client.post("/v1/forecast", json=EXAMPLE, headers=headers)
    second = client.post("/v1/forecast", json=EXAMPLE, headers=headers)
    assert first.status_code == 200 and first.json() == second.json()  # stateless: retries are safe
    expected = json.loads((Path(__file__).resolve().parents[1] / "contract" / "example_response.json").read_text())
    assert first.json() == expected
    assert first.headers["X-Request-ID"] == "erp-req-12345678"  # the ERP's ID carried through
    assert any(r.request_id == "erp-req-12345678" and "Forecast 2 series" in r.getMessage() for r in caplog.records)
    assert "TG-1001" not in caplog.text  # keys and quantities are the caller's data


def test_a_forged_request_id_is_replaced(client):
    answer = client.get("/health", headers={"X-Request-ID": "bad id\nfake log line"})
    assert answer.headers["X-Request-ID"] != "bad id\nfake log line"


def test_contract_errors_are_422_without_echoing_input(client):
    bad = {**EXAMPLE, "horizon": 0}
    answer = client.post("/v1/forecast", json=bad, headers={"X-Service-Token": TOKEN})
    assert answer.status_code == 422 and answer.json()["detail"][0]["loc"] == ["body", "horizon"]
    forced = {**EXAMPLE, "series": [{**EXAMPLE["series"][0], "method": "seasonal_naive"}]}
    refused = client.post("/v1/forecast", json=forced, headers={"X-Service-Token": TOKEN})
    assert refused.status_code == 422 and "needs at least" in refused.json()["detail"]
