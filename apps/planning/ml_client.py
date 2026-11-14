"""The ERP's client for ``ml_service`` (ADR 0014): one call, ``forecast(payload)``, with safe retries.

* **Off unless configured.** Without ``ML_SERVICE_URL`` forecasting is off and every call raises
  ``ServiceOff``, as the graph is off without ``NEO4J_URI``.
* **Retries are safe.** The service is stateless and deterministic, so a call that timed out or met a
  connection error or a 5xx is simply repeated (``ML_SERVICE_RETRIES`` attempts, 0.5 s, 1 s, 2 s apart).
  A 4xx is the ERP's mistake and is not retried.
* **Authenticated.** The shared secret ``ML_SERVICE_TOKEN`` goes in ``X-Service-Token``; the current
  request ID goes in ``X-Request-ID``, so the service's log line carries the same ID as the ERP's.
* **Logged without data.** Log lines show the URL path, status, attempt and timing, never the token,
  the request body or the response.
* Standard library only (``urllib``): the ERP gains no new dependency for this.

Tests replace ``post_json`` (the single network function) with a fake, so no test needs the service.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from django.conf import settings

from apps.common.logcontext import request_id

logger = logging.getLogger(__name__)
PATH = "/v1/forecast"


class ServiceOff(Exception):
    """Forecasting is not configured (no ``ML_SERVICE_URL``) or the service cannot be reached."""


class ServiceRejected(Exception):
    """The service refused the request (4xx): the ERP sent something the contract does not allow."""


def enabled() -> bool:
    """True when ``ML_SERVICE_URL`` is set."""
    return bool(settings.ML_SERVICE_URL)


def post_json(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    """POST ``body`` to ``url``; return (status, response body). The only function that uses the network."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        # The URL comes from settings, never from a user, and its scheme is checked in forecast().
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:  # a 4xx or 5xx still has a status and a body
        return exc.code, exc.read()


def forecast(payload: dict) -> dict:
    """Send a ``demand-forecast-request/v1`` and return the ``demand-forecast/v1`` answer (a dict).

    Raises ``ServiceOff`` when forecasting is off or the service stays unreachable after the retries,
    and ``ServiceRejected`` with the service's message for a 4xx.
    """
    base = settings.ML_SERVICE_URL.rstrip("/")
    if not base:
        raise ServiceOff("Forecasting is off: set ML_SERVICE_URL (and ML_SERVICE_TOKEN) to use ml_service.")
    if urlsplit(base).scheme not in ("http", "https"):
        raise ServiceOff("ML_SERVICE_URL must start with http:// or https://")
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Service-Token": settings.ML_SERVICE_TOKEN,
               "X-Request-ID": request_id.get()}
    attempts = max(1, settings.ML_SERVICE_RETRIES)
    last_problem = ""
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            status, answer = post_json(base + PATH, body, headers, settings.ML_SERVICE_TIMEOUT)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            status, answer, last_problem = None, b"", type(exc).__name__
        elapsed = (time.monotonic() - started) * 1000
        if status is not None and 200 <= status < 300:
            logger.info("ml_service %s answered %d in %.0f ms (attempt %d)", PATH, status, elapsed, attempt)
            return json.loads(answer)
        if status is not None and 400 <= status < 500:
            logger.warning("ml_service %s refused the request: %d", PATH, status)
            raise ServiceRejected(_detail(status, answer))
        last_problem = last_problem if status is None else f"HTTP {status}"
        logger.warning("ml_service %s attempt %d of %d failed (%s) after %.0f ms", PATH, attempt, attempts,
                       last_problem, elapsed)
        if attempt < attempts:
            time.sleep(0.5 * 2 ** (attempt - 1))
    raise ServiceOff(f"ml_service could not be reached after {attempts} attempt(s) ({last_problem}).")


def _detail(status: int, answer: bytes) -> str:
    """The service's own explanation of a 4xx, shortened; never the body we sent."""
    try:
        detail = json.loads(answer).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if status == 401:
        return "ml_service refused the token: ML_SERVICE_TOKEN must match on both sides."
    return f"ml_service refused the request ({status}): {json.dumps(detail)[:300] if detail else 'no detail'}"
