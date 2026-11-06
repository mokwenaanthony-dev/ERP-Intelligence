"""Request telemetry for ISO 27001 access tracking (design document, section 4).

Writes one line per request to the ``erp.requests`` logger, for example::

    2026-07-11 18:00:00 | INFO | middleware | User: finance_boss | Route: /api/finance/ledger/ |
    Method: POST | Latency: 0.0412s | Status: 201

Only the path is logged - never the query string, headers or body - so tokens,
passwords and personal data stay out of the logs (GDPR credential masking).
"""
import logging
import time

logger = logging.getLogger("erp.requests")


def _username(request) -> str:
    # DRF authenticates JWT users inside the view and copies the user back onto
    # the Django request, so by the time the response returns this is accurate.
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user.get_username()
    return "Anonymous"


class OperationalPerformanceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        latency = time.perf_counter() - started
        logger.info(
            "User: %s | Route: %s | Method: %s | Latency: %.4fs | Status: %s",
            _username(request),
            request.path,
            request.method,
            latency,
            response.status_code,
        )
        return response

    def process_exception(self, request, exception):
        logger.error(
            "Unhandled %s | User: %s | Route: %s | Method: %s",
            type(exception).__name__,
            _username(request),
            request.path,
            request.method,
            exc_info=exception,
        )
        return None
