# 0014. Demand forecasting in a stateless FastAPI `ml_service`; the ERP keeps histories and forecasts

- **Status:** Accepted, 2026-11-14 (Wave 8a)

## Context

Wave 8 (supply-chain management) starts with demand planning: MRP (Wave 8b) needs a forecast of
what customers will take. ADR 0001 foresaw one exception to the modular monolith, a separate service
for work with a different runtime profile, named `ml_service`. Forecasting is that work: numerical,
CPU-bound, with its own libraries (numpy) and its own release rhythm, and no need for the ERP's
database. A loose prototype (`main.py`, synthetic numbers) existed from before Wave 0.

## Decision

- **A separate FastAPI service in the same repository** (`ml_service/`, its own requirements,
  Dockerfile, tests and CI job `ml`). Same repository, unlike the MES (ADR 0009), because it has one
  client, the ERP, and changes with it; separate process and dependencies, because the ERP must not
  load numpy and the service must not load Django.
- **Stateless and deterministic.** The service keeps nothing between requests and uses no
  randomness: the same history always gives the same forecast. Retrying is therefore always safe
  (ADR 0010), and the ERP retries timeouts, connection errors and 5xx answers three times.
- **The ERP stays the system of record.** It builds each material's monthly demand from posted goods
  issues for deliveries (601; a reversal counts in the month of the delivery it reverses), sends the
  histories, checks the answer against the request and stores it as a numbered forecast run (FR).
  Runs are never changed; planning reads the latest. A run is fingerprinted (SHA-256 of the request):
  the same history at the same plant returns the stored run without calling the service again.
- **A versioned contract** (`demand-forecast-request/v1` → `demand-forecast/v1`, pydantic models in
  `ml_service/app/contract.py`). A worked example (`ml_service/contract/`) is tested by both sides:
  the service must answer it exactly, the ERP must accept it, and CI runs the built image against it.
- **Classical methods, chosen per series by backtest:** naive, moving average, seasonal naive, simple
  exponential smoothing, damped Holt trend, and Croston (SBA) for intermittent demand. Each is
  backtested on rolling origins; the lowest mean absolute error wins, ties to the simpler method.
  80 % intervals from the backtest errors, never below zero. The method and its error (MAE, MASE)
  are stored with each forecast so a planner can see why a number is what it is.
- **Authenticated and quiet.** A shared secret (`ML_SERVICE_TOKEN`, 16+ characters, constant-time
  comparison) on every forecast call; the service refuses to start without it. The ERP's request ID
  travels in `X-Request-ID`, and both sides log in the same format (ADR 0005) without bodies, keys,
  quantities or the token. Without `ML_SERVICE_URL` forecasting is off (API answers 503).

## Alternatives considered

- **Forecasting inside Django** (numpy in the ERP): simpler deployment, but heavy numerical work in
  the web workers and a dependency the rest of the ERP does not need.
- **A forecasting library (statsmodels, Prophet, a neural model):** more methods, but large
  dependencies and less explainable numbers for two-year monthly histories, where classical
  methods do as well (the M-competitions). The contract lets a stronger model replace these later.
- **The service reads the ERP database:** fewer bytes on the wire, but a second reader of the
  business tables that would break the boundary of ADR 0001; the request carries what it needs.
- **Forecasts in the knowledge graph now:** left to Wave 8c (supply-chain risk), which decides what
  the graph needs.

## Consequences

- MRP (8b) can plan against stored, explainable forecasts that do not change under it.
- The service can be scaled, replaced or upgraded on its own; a new model version shows in each run.
- Two shared settings must match (`ML_SERVICE_TOKEN` on both sides); a wrong token is reported as
  such (401 from the service, 502 from the ERP API).
- Forecasts are monthly per plant and material; weekly buckets, promotions and causal factors
  (price, campaigns) are not modelled yet.
