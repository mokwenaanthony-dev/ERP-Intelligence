"""Idempotent API requests (ADR 0010): repeating a request gives the same result as sending it once.

Every request that changes data (POST, PUT, PATCH, DELETE under ``/api/``) must carry an
``Idempotency-Key`` header, a unique value (e.g. a UUID) chosen by the client per intended operation
and sent again, unchanged, on every retry of that operation.

* First time: the request runs; a successful (2xx) response is stored with the key.
* Again, same key and same request: the stored response is returned, byte for byte, with
  ``Idempotent-Replayed: true``. Nothing runs a second time, so nothing is posted twice.
* Same key, different method, path or body: 422, nothing runs (the key was reused by mistake).
* Same key while the first request is still running: 409, retry shortly.
* No key: 428 Precondition Required.

Failed requests are not stored: postings are all-or-nothing transactions, so a failure changed
nothing and the retry runs again. Keys belong to the signed-in user and are purged after
``IDEMPOTENCY_TTL_DAYS`` (default 7) by a Celery beat task. ``/api/auth/`` is exempt: signing in
issues a new token by design, and logout is idempotent by itself.
"""
import hashlib
import json
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone

from .models import IdempotencyRecord

logger = logging.getLogger("erp.idempotency")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT_PREFIXES = ("/api/auth/",)
HEADER = "Idempotency-Key"
VALID_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
# A record still "running" after this long belongs to a request that died; a retry may take it over.
STALE_AFTER = timedelta(minutes=2)


def _json(status, detail):
    return HttpResponse(json.dumps({"detail": detail}), status=status, content_type="application/json")


def _user(request):
    """The signed-in user, found the same way DRF will find it, or None (the view then answers 401)."""
    forced = getattr(request, "_force_auth_user", None)  # the test client's force_authenticate
    if forced is not None:
        return forced
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:  # session (admin, browsable API)
        return user
    from rest_framework.exceptions import AuthenticationFailed
    from rest_framework_simplejwt.authentication import JWTAuthentication

    try:
        result = JWTAuthentication().authenticate(request)
    except AuthenticationFailed:
        return None
    return result[0] if result else None


def _fingerprint(request) -> str:
    digest = hashlib.sha256()
    for part in (request.method.encode(), request.get_full_path().encode(), request.body):
        digest.update(part)
        digest.update(b"\0")
    return digest.hexdigest()


def _replay(record, request):
    response = HttpResponse(record.response_body, status=record.status_code, content_type=record.content_type)
    response["Idempotent-Replayed"] = "true"
    logger.info("Replayed %s %s for Idempotency-Key %s (first answered %s)", request.method, request.path,
                record.key, record.completed_at)
    return response


class IdempotencyMiddleware:
    """Enforces ADR 0010 for every API request that changes data. Sits after the request-ID middleware,
    so replays and rejections are logged with the request ID."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (request.method not in UNSAFE_METHODS or not request.path.startswith("/api/")
                or request.path.startswith(EXEMPT_PREFIXES)):
            return self.get_response(request)
        user = _user(request)
        if user is None:
            return self.get_response(request)

        key = request.headers.get(HEADER, "").strip()
        if not key:
            logger.info("Refused %s %s: no %s header", request.method, request.path, HEADER)
            return _json(428, f"Send an {HEADER} header (a new UUID per operation, the same one on every "
                              "retry), so a repeated request cannot post twice.")
        if not VALID_KEY.match(key):
            return _json(422, f"{HEADER} must be 8-128 letters, digits, '-', '_', '.' or ':'.")

        fingerprint, now = _fingerprint(request), timezone.now()
        try:
            with transaction.atomic():
                record = IdempotencyRecord.objects.create(
                    user=user, key=key, method=request.method, path=request.get_full_path()[:255],
                    fingerprint=fingerprint, created_at=now,
                )
        except IntegrityError:
            record = IdempotencyRecord.objects.get(user=user, key=key)
            if record.fingerprint != fingerprint:
                logger.warning("Refused %s %s: Idempotency-Key %s was first used for %s %s", request.method,
                               request.path, key, record.method, record.path)
                return _json(422, f"This {HEADER} was already used for a different request. Use a new key for "
                                  "a new operation.")
            if record.status_code is not None:
                return _replay(record, request)
            if now - record.created_at < STALE_AFTER:
                return _json(409, "The first request with this key is still being processed. Retry shortly.")
            record.created_at = now  # the first attempt died without an answer: this retry takes over
            record.save(update_fields=["created_at"])

        response = self.get_response(request)
        if 200 <= response.status_code < 300 and not getattr(response, "streaming", False):
            record.status_code, record.completed_at = response.status_code, timezone.now()
            record.content_type = response.get("Content-Type", "")[:100]
            record.response_body = response.content.decode(response.charset or "utf-8", errors="replace")
            record.save(update_fields=["status_code", "completed_at", "content_type", "response_body"])
        else:
            record.delete()  # nothing was changed: the client may retry with the same key
        return response


def purge(days=None) -> int:
    """Delete keys older than the retention period. Returns how many were deleted."""
    days = days if days is not None else settings.IDEMPOTENCY_TTL_DAYS
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()
    logger.info("Purged %d idempotency key(s) older than %d day(s)", deleted, days)
    return deleted
