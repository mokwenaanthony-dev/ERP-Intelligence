# ml_service: demand forecasting for ERP-Intelligence (Wave 8a)

A small, stateless FastAPI service. The ERP sends monthly demand histories; the service picks a
forecasting method per series by backtest and returns forecasts with 80 % intervals. Why it is a
separate service, and how it is secured, is in [ADR 0014](../docs/decisions/0014-demand-forecasting-in-a-stateless-ml-service.md).

| Path | What |
|---|---|
| `app/forecasting.py` | The methods (naive, moving average, seasonal naive, SES, damped Holt, Croston SBA) and the backtest that chooses between them. numpy only |
| `app/contract.py` | `demand-forecast-request/v1` and `demand-forecast/v1` as pydantic models |
| `app/service.py` | Request in, response out, no web framework |
| `app/main.py` | FastAPI: `GET /health`, `POST /v1/forecast` (needs `X-Service-Token`) |
| `app/logs.py` | The ERP's log format, with the caller's request ID |
| `contract/` | The worked example both sides test against |

Run it with Docker Compose from the repository root (`docker compose up -d ml_service`; it needs
`ML_SERVICE_TOKEN` in `.env`), or on its own:

```bash
cd ml_service
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash; bin/activate elsewhere
pip install -r requirements-dev.txt
pytest                                                  # tests with coverage
ML_SERVICE_TOKEN=$(python -c "import secrets; print(secrets.token_hex(16))") uvicorn app.main:app --port 8001
```

Interactive API documentation: http://localhost:8001/docs. If the maths changes on purpose,
regenerate `contract/example_response.json` from `contract/example_request.json` and say so in the
pull request: the ERP and CI test against it.
