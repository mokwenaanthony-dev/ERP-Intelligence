"""Request telemetry for ISO 27001 access tracking (design document, section 4).

For every request this middleware:

1. assigns a **request ID** (or reuses a safe ``X-Request-ID`` sent by the caller), stores it in
   the logging context so every log line written during the request carries it, and returns it
   in the ``X-Request-ID`` response header;
2. writes one access line to the ``erp.requests`` logger, for example::

    2026-11-07 18:00:00 | INFO    | 3f9c2a7b1d4e | erp.requests | middleware.__call__:62 |
    User: finance_boss | Route: /api/finance/journal-entries/ | Method: POST | Latency: 0.0412s | Status: 201

3. logs any exception that escapes a view (outside DRF, e.g. the admin) with its full traceback.

Only the path is logged, never the query string, headers or body, so tokens, passwords and
personal data stay out of the logs (GDPR credential masking).
"""
import logging
import time

from .logcontext import new_request_id, request_id

logger = logging.getLogger("erp.requests")


def _username(request) -> str:
    # DRF authenticates JWT users inside the view and copies the user back onto
    # the Django request, so by the time the response returns this is accurate.
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user.get_username()
    return "Anonymous"


class OperationalPerformanceMiddleware:
    """Assign a request ID and write one access log line per request (see the module docstring)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = request_id.set(new_request_id(request.headers.get("X-Request-ID")))
        try:
            started = time.perf_counter()
            response = self.get_response(request)
            latency = time.perf_counter() - started
            response["X-Request-ID"] = request_id.get()
            # 5xx responses are faults; 4xx are the caller's mistakes and stay at INFO.
            level = logging.ERROR if response.status_code >= 500 else logging.INFO
            logger.log(
                level,
                "User: %s | Route: %s | Method: %s | Latency: %.4fs | Status: %s",
                _username(request),
                request.path,
                request.method,
                latency,
                response.status_code,
            )
            return response
        finally:
            request_id.reset(token)  # never leak an ID into the next request on this thread

    def process_exception(self, request, exception):
        """Log an exception that escaped a non-DRF view, with traceback; let Django build the 500."""
        logger.error(
            "Unhandled %s | User: %s | Route: %s | Method: %s",
            type(exception).__name__,
            _username(request),
            request.path,
            request.method,
            exc_info=exception,
        )
        return None
