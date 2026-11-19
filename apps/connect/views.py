"""REST API of side-by-side mode (Wave 10b, ADR 0020), under ``/api/connect/``.

    GET  sources/                          the mappings and, per object, the columns each needs
    POST imports/   multipart: company_code, source, object, file (CSV)
                    201 a new batch; 200 the same file again (the first batch, nothing loaded twice);
                    400 a file that cannot be read (nothing loaded)
    GET  imports/?company_code=1000        the batches, newest first (without row details)
    GET  imports/<id>/                     one batch with the result and reason of every row

Importing is for the finance manager and administrators; the other business roles may read.
"""
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import ConnectPermission
from apps.organization.models import CompanyCode

from . import services
from .mappings import FIELDS, MAPPINGS, FileError
from .models import ImportBatch, ObjectType

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file: an export of open documents, not a data warehouse


class ImportInput(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    source = serializers.ChoiceField(choices=sorted(MAPPINGS))
    object = serializers.ChoiceField(choices=ObjectType.choices)
    file = serializers.FileField()


class SourceView(APIView):
    permission_classes = [ConnectPermission]

    def get(self, request):
        objects = [{"object": value, "label": label,
                    "columns": [{"field": f, "required": r} for f, r in FIELDS[value]]}
                   for value, label in ObjectType.choices]
        return Response({"sources": [{"name": m.name, "title": m.title} for m in MAPPINGS.values()],
                         "objects": objects, "load_in_this_order": ObjectType.values})


class ImportView(APIView):
    permission_classes = [ConnectPermission]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        code = request.query_params.get("company_code")
        batches = ImportBatch.objects.filter(company_code__code=code).select_related("company_code", "imported_by")
        found = [{k: v for k, v in services.batch_view(b).items() if k != "results"} for b in batches[:200]]
        return Response({"count": len(found), "imports": found})

    def post(self, request):
        given = ImportInput(data=request.data)
        given.is_valid(raise_exception=True)
        data = given.validated_data
        upload = data["file"]
        if upload.size > MAX_BYTES:
            raise serializers.ValidationError({"file": ["Larger than 5 MB: split the export."]})
        try:
            batch, created = services.import_file(data["company_code"], source=data["source"],
                                                  object_type=data["object"], content=upload.read(),
                                                  file_name=upload.name, user=request.user)
        except FileError as exc:
            raise serializers.ValidationError({"file": [str(exc)]}) from exc
        return Response(services.batch_view(batch), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ImportDetailView(APIView):
    permission_classes = [ConnectPermission]

    def get(self, request, pk):
        batch = ImportBatch.objects.select_related("company_code", "imported_by").filter(pk=pk).first()
        if batch is None:
            raise NotFound("No such import.")
        return Response(services.batch_view(batch))
