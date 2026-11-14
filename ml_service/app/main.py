"""The HTTP layer of ml_service (FastAPI): authentication, request IDs, logging, two endpoints.

    GET  /health        is the service up? (no token: Docker's health check calls it)
    POST /v1/forecast   demand-forecast-request/v1 in, demand-forecast/v1 out (``contract.py``)

Run it with ``uvicorn app.main:app --host 0.0.0.0 --port 8001`` (the Dockerfile does).

**Authentication.** Only the ERP calls this service. Every forecast request must carry the shared
secret ``ML_SERVICE_TOKEN`` in the ``X-Service-Token`` header, compared in constant time. The service
refuses to start without a token, so it can never run open by mistake.

**Idempotent by construction.** The service keeps no state and the maths has no randomness, so a
repeated request gets the same answer and changes nothing: the ERP may retry freely (ADR 0010).

**Logging.** One line per request (method, path, status, milliseconds) with the caller's request ID;
rejected requests log why, with the file and line; unexpected errors log a traceback and return only
the request ID. Bodies, tokens and series keys are never logged.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
import traceback

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, logs
from .contract import ForecastRequest, ForecastResponse
from .service import handle

logs.configure()
logger = logging.getLogger("app.api")


def _token() -> str:
    token = os.environ.get("ML_SERVICE_TOKEN", "")
    if len(token) < 16:
        # Fail loudly at start-up rather than serve forecasts to anyone who can reach the port.
        raise RuntimeError("ML_SERVICE_TOKEN must be set to a secret of at least 16 characters")
    return token


TOKEN = _token()
app = FastAPI(title="ERP-Intelligence ml_service", version=__version__,
              description="Demand forecasting for the ERP (contract demand-forecast/v1, ADR 0014).")


@app.middleware("http")
async def request_log(request: Request, call_next):
    """Set the request ID, time the call, log one line, and echo the ID in ``X-Request-ID``."""
    request_id = logs.set_request_id(request.headers.get("X-Request-ID"))
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # anything the handlers did not catch: log it here, answer 500
        logger.exception("Unexpected error in %s %s", request.method, request.url.path)
        response = JSONResponse({"detail": "Internal error", "request_id": request_id}, status_code=500)
    # The path only, never the query string (it could carry data).
    logger.info("%s %s -> %d in %.0f ms", request.method, request.url.path, response.status_code,
                (time.perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    """A request that breaks the contract: 422 with pydantic's field messages (no input echoed back)."""
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
    logger.warning("Rejected %s: %d contract error(s), first at %s", request.url.path, len(errors),
                   ".".join(str(p) for p in errors[0]["loc"]) if errors else "-")
    return JSONResponse({"detail": errors, "request_id": logs.current_request_id()}, status_code=422)


def require_token(x_service_token: str = Header(default="")) -> None:
    """Refuse calls without the shared secret (401), comparing in constant time."""
    if not hmac.compare_digest(x_service_token.encode(), TOKEN.encode()):
        logger.warning("Refused a call without a valid service token")
        raise HTTPException(status_code=401, detail="A valid X-Service-Token header is required")


@app.get("/health")
def health() -> dict:
    """Liveness for Docker and the ERP: no token, no work."""
    return {"status": "ok", "version": __version__}


@app.post("/v1/forecast", response_model=ForecastResponse, response_model_by_alias=True,
          dependencies=[Depends(require_token)])
def forecast(request: ForecastRequest) -> ForecastResponse:
    """Forecast every series in the request (contract v1). Pure: retrying is always safe."""
    try:
        return handle(request)
    except ValueError as exc:  # a rule the maths enforces beyond the schema
        where = traceback.extract_tb(exc.__traceback__)[-1]  # where the rule was raised, for troubleshooting
        logger.warning("Rejected forecast request at %s:%d: %s", os.path.basename(where.filename), where.lineno, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
