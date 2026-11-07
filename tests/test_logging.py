"""Troubleshooting logs: request IDs, error locations, business-rule warnings and safe 500s.

These tests pin down what an operator relies on when something goes wrong: every line of one
request shares an ID, a rejected business rule says which file and line rejected it, and an
unexpected error is logged with its traceback while the caller only sees a reference to quote.
"""
import logging
from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.common.logcontext import RequestIdFilter, error_location, new_request_id, request_id
from apps.common.middleware import OperationalPerformanceMiddleware
from apps.finance.models import PostingPeriod
from apps.finance.services import PostingError
from erp_system import celery as celery_module

from .test_finance_api import JOURNAL, invoice_payload

pytestmark = pytest.mark.django_db


@pytest.fixture
def accountant(client_for):
    return client_for(Role.ACCOUNTANT)


# --- Request IDs -----------------------------------------------------------------------------

def test_every_response_carries_a_request_id(accountant):
    response = accountant.get("/api/health/")
    assert len(response["X-Request-ID"]) == 12


def test_a_safe_caller_request_id_is_reused(accountant):
    response = accountant.get("/api/health/", HTTP_X_REQUEST_ID="mes-line1:42")
    assert response["X-Request-ID"] == "mes-line1:42"


@pytest.mark.parametrize("supplied", ["has spaces", "line\nbreak", "x" * 65, ""])
def test_an_unsafe_request_id_is_replaced(supplied):
    # Anything that could forge or break a log line is ignored and a fresh ID is used.
    generated = new_request_id(supplied)
    assert generated != supplied and len(generated) == 12


def test_log_lines_of_one_request_share_its_id(accountant, caplog):
    # Attach the filter to the capture handler, as settings.LOGGING does for the real handlers.
    caplog.handler.addFilter(RequestIdFilter())
    with caplog.at_level(logging.INFO):
        response = accountant.get("/api/health/", HTTP_X_REQUEST_ID="trace-me")
    assert response.status_code == 200
    record = next(r for r in caplog.records if r.name == "erp.requests")
    assert record.request_id == "trace-me"


def test_the_request_id_does_not_leak_into_the_next_request(accountant):
    accountant.get("/api/health/", HTTP_X_REQUEST_ID="first-call")
    assert request_id.get() == "-"


# --- Error locations -------------------------------------------------------------------------

def _raise_rule():
    raise PostingError("rule broken")


def test_error_location_names_file_line_and_function():
    try:
        _raise_rule()
    except PostingError as exc:
        where = error_location(exc)
    assert where.startswith("tests/test_logging.py:")
    assert where.endswith("in _raise_rule")


def test_error_location_without_a_traceback():
    assert error_location(ValueError("never raised")) == "unknown location"


def test_business_rule_rejection_is_logged_with_where_it_was_raised(demo, accountant, caplog):
    PostingPeriod.objects.filter(fiscal_year__year=2027, number=3).update(status="CLOSED")
    with caplog.at_level(logging.INFO, logger="erp.api"):
        response = accountant.post(JOURNAL, invoice_payload(demo), format="json")
    assert response.status_code == 400

    warning = next(r for r in caplog.records if r.name == "erp.api" and r.levelno == logging.WARNING)
    message = warning.getMessage()
    assert "is closed" in message
    assert "JournalEntryViewSet POST /api/finance/journal-entries/" in message
    assert "raised at apps/finance/services.py:" in message  # the exact line of the rule


def test_validation_errors_are_logged_at_info_not_as_faults(accountant, caplog):
    with caplog.at_level(logging.INFO, logger="erp.api"):
        response = accountant.post(JOURNAL, {"lines": []}, format="json")
    assert response.status_code == 400
    levels = {r.levelno for r in caplog.records if r.name == "erp.api"}
    assert levels == {logging.INFO}


# --- Unexpected errors -----------------------------------------------------------------------

class _BrokenView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        raise RuntimeError("database connection lost")


def test_unexpected_error_returns_safe_500_with_reference(caplog):
    middleware = OperationalPerformanceMiddleware(_BrokenView.as_view())
    with caplog.at_level(logging.INFO):
        response = middleware(RequestFactory().get("/api/broken/"))

    assert response.status_code == 500
    response.render()
    body = response.data
    assert body["request_id"] == response["X-Request-ID"]
    assert "database connection lost" not in str(body)  # internals never reach the caller

    error = next(r for r in caplog.records if r.name == "erp.api" and r.levelno == logging.ERROR)
    assert error.exc_info is not None  # full traceback, with line numbers, goes to the log
    assert "raised at tests/test_logging.py:" in error.getMessage()
    access = next(r for r in caplog.records if r.name == "erp.requests")
    assert access.levelno == logging.ERROR and "Status: 500" in access.getMessage()


# --- Celery tasks ----------------------------------------------------------------------------

def test_task_log_lines_carry_the_task_id():
    task = SimpleNamespace(name="graph.process_outbox")
    celery_module._start(task_id="abcdef1234567890", task=task)
    assert request_id.get() == "task:abcdef12"
    celery_module._finish(task_id="abcdef1234567890", task=task, state="SUCCESS")
    assert request_id.get() == "-"


def test_task_failure_is_logged_with_location(caplog):
    try:
        _raise_rule()
    except PostingError as exc:
        with caplog.at_level(logging.ERROR, logger="erp.tasks"):
            celery_module._failed(task_id="t1", exception=exc, sender=SimpleNamespace(name="analytics.refresh_views"))
    record = caplog.records[-1]
    assert "analytics.refresh_views failed: rule broken" in record.getMessage()
    assert "in _raise_rule" in record.getMessage()
    assert record.exc_info is not None


# --- Business events -------------------------------------------------------------------------

def test_posting_writes_a_business_event_line(demo, accountant, caplog):
    with caplog.at_level(logging.INFO, logger="apps.finance.services"):
        response = accountant.post(JOURNAL, invoice_payload(demo), format="json")
    assert response.status_code == 201, response.data
    line = next(r.getMessage() for r in caplog.records if r.name == "apps.finance.services")
    assert line.startswith(f"Posted DR 1000/{response.data['document_number']}")
