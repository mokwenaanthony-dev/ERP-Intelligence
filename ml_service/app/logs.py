"""Logging for ml_service, in the ERP's format (ADR 0005), so one grep follows a call across both.

    2026-11-14 10:02:47 | INFO    | 3f9c2a7b1d4e | app.service | service.handle:42 | Forecast 12 series ...

The third field is the request ID: the ERP sends its own in ``X-Request-ID`` and the service logs it,
so ``grep <id>`` on the ERP's log and on this service's output shows the same call. A caller's ID is
used only if it matches a strict pattern, so nobody can forge log lines through the header.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import uuid

FORMAT = "%(asctime)s | %(levelname)-7s | %(request_id)s | %(name)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
SAFE_ID = re.compile(r"^[A-Za-z0-9:_-]{8,64}$")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_request_id(incoming: str | None) -> str:
    """Use the caller's request ID if it is safe, otherwise a new one; returns the ID in force."""
    value = incoming if incoming and SAFE_ID.match(incoming) else uuid.uuid4().hex[:12]
    _request_id.set(value)
    return value


def current_request_id() -> str:
    """The request ID of the call being handled ("-" outside a request)."""
    return _request_id.get()


_base_factory = logging.getLogRecordFactory()


def _record_with_request_id(*args, **kwargs) -> logging.LogRecord:
    """Every log record, from any logger and to any handler, carries the current request ID."""
    record = _base_factory(*args, **kwargs)
    record.request_id = _request_id.get()
    return record


def configure() -> None:
    """Send every log line to the console (``docker compose logs ml_service``) in the shared format."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    logging.setLogRecordFactory(_record_with_request_id)
    root = logging.getLogger()
    # Replace only our own handler (configure may run twice, e.g. on reload); others, such as a test
    # runner's log capture, stay.
    handler.ml_service = True
    root.handlers[:] = [h for h in root.handlers if not getattr(h, "ml_service", False)] + [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    # uvicorn's access log repeats every request without our request ID; our middleware logs instead.
    logging.getLogger("uvicorn.access").disabled = True
