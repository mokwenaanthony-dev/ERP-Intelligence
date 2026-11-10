"""Knowledge graph API: Customer 360, document flow, supplier dependency, where used, KG-RAG context, status
and rebuild.

Every query is filtered by the caller's role (``access.py``), so the graph never shows a user more
than the relational API would. Returns 503 when the graph is switched off (no ``NEO4J_URI``).
"""
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import GraphAdminPermission, GraphPermission
from apps.finance.views import _company_code

from . import processor, queries
from .access import allowed_labels
from .projection import ALL_LABELS


class GraphDisabled(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The knowledge graph is off: set NEO4J_URI (Docker Compose does this)."


class _GraphView(APIView):
    permission_classes = [GraphPermission]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.GRAPH_ENABLED:
            raise GraphDisabled()
        self.allowed = allowed_labels(request.user)

    def require(self, *labels):
        missing = set(labels) - self.allowed
        if missing:
            raise PermissionDenied(f"Your role cannot see {', '.join(sorted(missing))} in the graph.")


class StatusView(APIView):
    """GET /api/graph/status/ - is the graph on, and how far behind is it?"""

    permission_classes = [GraphPermission]

    def get(self, request):
        return Response(processor.status())


class Customer360View(_GraphView):
    """GET /api/graph/customer-360/<number>/ - one business partner across sales, billing, payments, purchasing."""

    def get(self, request, number):
        self.require("BusinessPartner", "SalesOrder")
        result = queries.customer_360(number, self.allowed)
        if result is None:
            return Response({"detail": f"Business partner {number} is not in the graph."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(result)


class DocumentFlowView(_GraphView):
    """GET /api/graph/document-flow/?company_code=1000&document=SO00000001"""

    def get(self, request):
        company_code = _company_code(request)
        number = request.query_params.get("document", "").strip()
        if not number:
            raise serializers.ValidationError({"document": "Give a document number, e.g. SO00000001."})
        try:
            result = queries.document_flow(company_code.code, number, self.allowed)
        except ValueError as exc:
            raise serializers.ValidationError({"document": str(exc)}) from exc
        except PermissionError as exc:
            raise PermissionDenied(f"Your role cannot see {exc} documents.") from exc
        if result is None:
            return Response({"detail": f"Document {number} is not in the graph."}, status=status.HTTP_404_NOT_FOUND)
        return Response(result)


class SupplierDependencyView(_GraphView):
    """GET /api/graph/supplier-dependency/?company_code=1000 - single-source materials and who they affect."""

    def get(self, request):
        self.require("BusinessPartner", "PurchaseOrder")
        company_code = _company_code(request)
        return Response({"company_code": company_code.code,
                         "materials": queries.supplier_dependency(company_code.code, self.allowed)})


class WhereUsedView(_GraphView):
    """GET /api/graph/where-used/<material>/ - products that use a material at any BOM level, and the
    open sales orders for them (shown only to roles that may see sales)."""

    def get(self, request, material):
        self.require("BOM", "Material")
        result = queries.where_used(material.upper(), self.allowed)
        if result is None:
            raise NotFound(f"Material {material} is not in the graph.")
        return Response(result)


class ContextView(_GraphView):
    """GET /api/graph/context/?label=SalesOrder&key=1000/SO00000001[&depth=2]

    Knowledge-graph retrieval (KG-RAG): grounded facts about one entity for an AI agent, limited to
    what the caller's role may see.
    """

    def get(self, request):
        label, key = request.query_params.get("label", ""), request.query_params.get("key", "")
        if not label or not key:
            raise serializers.ValidationError("label and key are required, e.g. label=SalesOrder&key=1000/SO00000001.")
        if label not in ALL_LABELS:
            raise serializers.ValidationError({"label": f"Unknown label {label}."})
        try:
            depth = int(request.query_params.get("depth", 2))
        except ValueError:
            raise serializers.ValidationError({"depth": "Must be 1, 2 or 3."}) from None
        try:
            result = queries.context(label, key, self.allowed, depth=depth)
        except PermissionError as exc:
            raise PermissionDenied(f"Your role cannot see {exc} in the graph.") from exc
        if result is None:
            return Response({"detail": f"{label} {key} is not in the graph."}, status=status.HTTP_404_NOT_FOUND)
        return Response(result)


class RebuildView(APIView):
    """POST /api/graph/rebuild/ - rebuild the whole graph from the ERP tables (administrators)."""

    permission_classes = [GraphAdminPermission]

    def post(self, request):
        if not settings.GRAPH_ENABLED:
            raise GraphDisabled()
        return Response({"projected": processor.rebuild()})
