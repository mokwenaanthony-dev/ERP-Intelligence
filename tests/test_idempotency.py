"""Idempotency (ADR 0010): repeating an operation gives the same result as doing it once.

The journal entry is the test subject because a double posting there is the costliest mistake:
retrying a request must never post twice, and the caller must get the same answer both times.
"""
import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Role
from apps.common import idempotency
from apps.common.models import IdempotencyRecord
from apps.common.tasks import purge_idempotency_keys
from apps.finance.models import JournalEntry

from .conftest import IdempotentAPIClient
from .test_finance_api import JOURNAL, invoice_payload

pytestmark = pytest.mark.django_db
KEY = "4f9c2a7b-1d4e-4a8e-9b1a-0c2d3e4f5a6b"


@pytest.fixture
def accountant(make_user):
    """A client that sends only the keys the test gives it."""
    client = APIClient()
    client.force_authenticate(make_user(Role.ACCOUNTANT))
    return client


def post(client, payload, key=KEY):
    return client.post(JOURNAL, payload, format="json", HTTP_IDEMPOTENCY_KEY=key)


def test_a_retry_returns_the_first_answer_and_posts_once(demo, accountant):
    before = JournalEntry.objects.count()
    first = post(accountant, invoice_payload(demo))
    assert first.status_code == 201, first.data
    retry = post(accountant, invoice_payload(demo))
    assert retry.status_code == 201
    assert retry.content == first.content  # byte for byte: same document number, same lines
    assert retry["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first
    assert JournalEntry.objects.count() == before + 1


def test_a_request_without_a_key_is_refused(demo, accountant):
    before = JournalEntry.objects.count()
    response = accountant.post(JOURNAL, invoice_payload(demo), format="json")
    assert response.status_code == 428
    assert "Idempotency-Key" in json.loads(response.content)["detail"]
    assert JournalEntry.objects.count() == before


@pytest.mark.parametrize("key", ["short", "has spaces in it", "x" * 129])
def test_badly_formed_keys_are_refused(demo, accountant, key):
    assert post(accountant, invoice_payload(demo), key=key).status_code == 422


def test_reusing_a_key_for_a_different_request_is_refused(demo, accountant):
    post(accountant, invoice_payload(demo))
    before = JournalEntry.objects.count()
    other = invoice_payload(demo)
    other["reference"] = "DIFFERENT"
    response = post(accountant, other)
    assert response.status_code == 422 and "different request" in json.loads(response.content)["detail"]
    assert JournalEntry.objects.count() == before


def test_a_retry_while_the_first_is_running_waits(demo, accountant):
    post(accountant, invoice_payload(demo))
    record = IdempotencyRecord.objects.get(key=KEY)
    record.status_code = None  # as if the first request were still running
    record.save()
    assert post(accountant, invoice_payload(demo)).status_code == 409

    # A "running" record left behind by a request that died is taken over after a while.
    record.created_at = timezone.now() - timedelta(minutes=5)
    record.save()
    assert post(accountant, invoice_payload(demo)).status_code == 201


def test_failed_requests_are_not_stored_and_can_be_retried(demo, accountant):
    bad = invoice_payload(demo)
    bad["lines"] = []
    assert post(accountant, bad).status_code == 400
    assert not IdempotencyRecord.objects.filter(key=KEY).exists()
    assert post(accountant, invoice_payload(demo)).status_code == 201  # same key, now a valid request


def test_keys_belong_to_one_user(demo, accountant, make_user):
    other = APIClient()
    other.force_authenticate(make_user(Role.FINANCE_MANAGER))
    before = JournalEntry.objects.count()
    assert post(accountant, invoice_payload(demo)).status_code == 201
    assert post(other, invoice_payload(demo)).status_code == 201  # the same key from someone else is theirs
    assert JournalEntry.objects.count() == before + 2


def test_reads_signin_and_anonymous_requests_need_no_key(demo, anon_client, accountant, make_user):
    assert accountant.get(JOURNAL).status_code == 200
    assert APIClient().post(JOURNAL, {}, format="json").status_code == 401  # the view answers, not the key check
    user = make_user(Role.VIEWER, username="signin")
    user.set_password("Str0ng-Test-Pass!")
    user.save()
    token = APIClient().post("/api/auth/token/", {"username": "signin", "password": "Str0ng-Test-Pass!"},
                             format="json")
    assert token.status_code == 200


def test_jwt_clients_are_recognised(demo, make_user):
    user = make_user(Role.ACCOUNTANT, username="jwt-user")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    assert client.post(JOURNAL, invoice_payload(demo), format="json").status_code == 428
    first = post(client, invoice_payload(demo))
    assert first.status_code == 201 and post(client, invoice_payload(demo)).content == first.content
    bad = APIClient()
    bad.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
    assert bad.post(JOURNAL, {}, format="json").status_code == 401


def test_logout_twice_succeeds_both_times(make_user):
    user = make_user(Role.VIEWER, username="leaver")
    refresh = str(RefreshToken.for_user(user))
    client = APIClient()
    assert client.post("/api/auth/logout/", {"refresh": refresh}, format="json").status_code == 200
    assert client.post("/api/auth/logout/", {"refresh": refresh}, format="json").status_code == 200
    assert client.post("/api/auth/logout/", {"refresh": "garbage"}, format="json").status_code == 401


def test_old_keys_are_purged(demo, accountant):
    post(accountant, invoice_payload(demo))
    post(accountant, invoice_payload(demo), key="second-key-0001")
    IdempotencyRecord.objects.filter(key=KEY).update(created_at=timezone.now() - timedelta(days=8))
    assert purge_idempotency_keys() == {"deleted": 1}
    assert idempotency.purge(days=0) == 1
    assert not IdempotencyRecord.objects.exists()


def test_replays_are_logged(demo, accountant, caplog):
    post(accountant, invoice_payload(demo))
    with caplog.at_level("INFO", logger="erp.idempotency"):
        post(accountant, invoice_payload(demo))
    assert any("Replayed POST /api/finance/journal-entries/" in r.getMessage() for r in caplog.records)


def test_the_test_client_sends_a_fresh_key_each_time(demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    before = JournalEntry.objects.count()
    accountant.post(JOURNAL, invoice_payload(demo), format="json")
    accountant.post(JOURNAL, invoice_payload(demo), format="json")
    assert JournalEntry.objects.count() == before + 2  # two keys = two intended operations
    assert isinstance(IdempotentAPIClient(), APIClient)


def test_commands_are_idempotent(demo):
    call_command("setup_demo_production", stdout=StringIO())
    counts = (JournalEntry.objects.count(), IdempotencyRecord.objects.count())
    out = StringIO()
    call_command("setup_demo_production", stdout=out)
    call_command("setup_demo_company", "--fiscal-year", "2027", stdout=StringIO())
    assert (JournalEntry.objects.count(), IdempotencyRecord.objects.count()) == counts
    assert "already exists" in out.getvalue()
