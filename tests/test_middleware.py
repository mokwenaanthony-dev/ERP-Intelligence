import logging

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Role
from apps.common.middleware import OperationalPerformanceMiddleware

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def test_request_line_has_user_route_method_latency_status(anon_client, make_user, caplog):
    make_user(Role.SALES_REP, username="finance_boss")
    access = anon_client.post(reverse("token_obtain_pair"), {"username": "finance_boss", "password": PASSWORD},
                              format="json").data["access"]
    anon_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    with caplog.at_level(logging.INFO, logger="erp.requests"):
        anon_client.get(reverse("sales-order-list") + "?search=secret-value")

    lines = [r.getMessage() for r in caplog.records if r.name == "erp.requests"]
    assert lines, "middleware wrote nothing"
    line = lines[-1]
    assert "User: finance_boss" in line  # JWT user resolved, not 'Anonymous'
    assert "Route: /api/sales/orders/" in line
    assert "Method: GET" in line
    assert "Status: 200" in line
    assert "Latency: " in line
    assert "secret-value" not in line  # query strings are never logged


def test_login_payload_is_never_logged(anon_client, make_user, caplog):
    make_user(Role.SALES_REP, username="rep")
    with caplog.at_level(logging.INFO, logger="erp.requests"):
        anon_client.post(reverse("token_obtain_pair"), {"username": "rep", "password": PASSWORD}, format="json")
    assert PASSWORD not in caplog.text
    assert "User: Anonymous" in caplog.text


def test_unhandled_exception_is_logged(caplog):
    middleware = OperationalPerformanceMiddleware(lambda request: HttpResponse())
    request = RequestFactory().get("/boom/")
    with caplog.at_level(logging.ERROR, logger="erp.requests"):
        assert middleware.process_exception(request, ValueError("bad")) is None
    assert "Unhandled ValueError" in caplog.text
    assert "Route: /boom/" in caplog.text
