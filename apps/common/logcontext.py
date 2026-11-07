"""Logging helpers shared by the whole system: request IDs and error locations.

Every log line carries a **request ID** so all the lines written while handling one API call (or
one Celery task) can be found together, for example with ``grep a1b2c3d4e5f6 logs/erp.log``:

    2026-11-07 18:40:12 | INFO    | a1b2c3d4e5f6 | apps.sales.services | services.post_invoice:331 | ...

How it fits together:

* ``OperationalPerformanceMiddleware`` gives each request an ID (or reuses a valid ``X-Request-ID``
  sent by the caller), stores it in ``request_id`` (a context variable, so concurrent requests and
  threads never mix them up) and returns it in the ``X-Request-ID`` response header.
* Celery signals (``erp_system/celery.py``) do the same for background tasks: ``task:<id>``.
* ``RequestIdFilter`` copies the current ID onto every log record, so the formatter in
  ``settings.LOGGING`` can print ``%(request_id)s``. Outside a request it prints ``-``.
* ``error_location`` turns an exception into ``file.py:line in function`` for the place it was
  raised, so a log line says exactly where a business rule fired.
"""
import contextvars
import logging
import re
import traceback
import uuid
from pathlib import Path

request_id = contextvars.ContextVar("request_id", default="-")

# A caller-supplied ID is reused only if it is short and plain, so it cannot inject text into logs.
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def new_request_id(supplied: str | None = None) -> str:
    """The caller's X-Request-ID when it is safe to log, otherwise a fresh 12-character ID."""
    if supplied and VALID_REQUEST_ID.match(supplied):
        return supplied
    return uuid.uuid4().hex[:12]


class RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` to every log record (never filters anything out)."""

    def filter(self, record):
        """Copy the current request ID onto the record so the formatter can print it."""
        record.request_id = request_id.get()
        return True


def error_location(exc: BaseException) -> str:
    """Where an exception was raised, as ``apps/sales/services.py:212 in post_delivery``.

    Prefers the innermost frame inside this project's own code, so library frames (Django, DRF)
    do not hide the line that actually broke the rule. Paths are shown relative to the project.
    """
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not frames:
        return "unknown location"
    project = [f for f in frames if _project_path(f.filename) is not None]
    frame = (project or frames)[-1]
    shown = _project_path(frame.filename) or Path(frame.filename).name
    return f"{shown}:{frame.lineno} in {frame.name}"


# The folder that holds manage.py; library code (site-packages) is never inside it.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_path(filename: str) -> str | None:
    """``filename`` relative to the project root, or None for library and virtualenv files."""
    try:
        relative = Path(filename).resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    if relative.parts and relative.parts[0] in (".venv", "venv", "env"):
        return None
    return relative.as_posix()
