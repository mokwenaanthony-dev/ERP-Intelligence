"""Reusable view building blocks for all ERP modules."""
from django.db import connection
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


class AuditedViewMixin:
    """Stamps created_by / updated_by with the requesting user."""

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class ProtectedDestroyMixin:
    """Turns a delete blocked by on_delete=PROTECT into 409 Conflict instead of a 500."""

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "This record is used by other documents and cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    """Liveness and database check for Docker and load balancers."""
    try:
        connection.ensure_connection()
        database = "ok"
    except Exception:  # pragma: no cover - only reached when the DB is down
        database = "unavailable"
    http_status = status.HTTP_200_OK if database == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE
    return Response({"status": "ok" if database == "ok" else "degraded", "database": database}, status=http_status)
