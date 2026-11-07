"""One place where every API error is logged before the response goes out.

Configured as ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``. DRF calls it whenever a view raises.

* **Business rule rejected** (a ``PostingError`` such as "Posting period 2027/03 is closed", turned into a
  400 by a serializer or view): logged at WARNING with the rule's message and the file and line
  where it was raised, e.g. ``apps/finance/services.py:232 in _create_entry``.
* **Other client errors** (validation, authentication, permission, not found): logged at INFO
  with the status and a short summary. They are normal traffic, not faults.
* **Unexpected errors** (bugs, database outages): logged at ERROR with the full traceback, line
  numbers included, in both ``erp.log`` and ``errors.log``. The caller gets a plain 500 with the
  request ID to quote; internal details never reach the client.
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.finance.services import PostingError

from .logcontext import error_location, request_id

logger = logging.getLogger("erp.api")


def _summary(data, limit=300) -> str:
    text = str(data)
    return text if len(text) <= limit else text[:limit] + "..."


def api_exception_handler(exc, context):
    """Log every API error at the right level, then return DRF's normal response (or a safe 500)."""
    view = context.get("view")
    request = context.get("request")
    where = f"{type(view).__name__} {request.method} {request.path}" if view and request else "API"
    response = drf_exception_handler(exc, context)

    if response is None:
        logger.error("Unexpected %s in %s: %s (raised at %s)", type(exc).__name__, where, exc,
                     error_location(exc), exc_info=exc)
        return Response(
            {"detail": "An unexpected error occurred. Quote this reference when reporting it.",
             "request_id": request_id.get()},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    cause = exc.__cause__
    if isinstance(cause, PostingError):
        logger.warning("Business rule rejected in %s: %s (raised at %s)", where, cause, error_location(cause))
    else:
        logger.info("%s %s in %s: %s", response.status_code, type(exc).__name__, where, _summary(response.data))
    return response
