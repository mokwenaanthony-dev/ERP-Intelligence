import pytest
from django.urls import reverse

from apps.accounts.models import Role

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def _login(client, username):
    return client.post(reverse("token_obtain_pair"), {"username": username, "password": PASSWORD}, format="json")


def test_login_returns_access_and_refresh_tokens(anon_client, make_user):
    make_user(Role.SALES_REP, username="rep")
    response = _login(anon_client, "rep")
    assert response.status_code == 200
    assert {"access", "refresh"} <= response.data.keys()


def test_login_rejects_wrong_password(anon_client, make_user):
    make_user(Role.SALES_REP, username="rep")
    response = anon_client.post(
        reverse("token_obtain_pair"), {"username": "rep", "password": "wrong"}, format="json"
    )
    assert response.status_code == 401


def test_bearer_token_grants_access(anon_client, make_user):
    make_user(Role.SALES_REP, username="rep")
    access = _login(anon_client, "rep").data["access"]
    anon_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert anon_client.get(reverse("sales-order-list")).status_code == 200


def test_unauthenticated_request_is_rejected(anon_client):
    assert anon_client.get(reverse("sales-order-list")).status_code == 401


def test_refresh_issues_new_access_token(anon_client, make_user):
    make_user(Role.SALES_REP, username="rep")
    refresh = _login(anon_client, "rep").data["refresh"]
    response = anon_client.post(reverse("token_refresh"), {"refresh": refresh}, format="json")
    assert response.status_code == 200
    assert "access" in response.data


def test_logout_blacklists_refresh_token(anon_client, make_user):
    make_user(Role.SALES_REP, username="rep")
    refresh = _login(anon_client, "rep").data["refresh"]
    assert anon_client.post(reverse("token_blacklist"), {"refresh": refresh}, format="json").status_code == 200
    assert anon_client.post(reverse("token_refresh"), {"refresh": refresh}, format="json").status_code == 401


def test_health_endpoint_needs_no_login(anon_client):
    response = anon_client.get(reverse("health"))
    assert response.status_code == 200
    assert response.data == {"status": "ok", "database": "ok"}
