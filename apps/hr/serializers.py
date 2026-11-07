"""Serializers for HR and payroll: validate API input and shape API output.

Input serializers check types and required fields only; business rules live in ``services.py``.
Output serializers are read-only views of posted documents.
"""
from rest_framework import serializers

from apps.controlling.models import CostCenter
from apps.finance.services import PostingError
from apps.organization.models import CompanyCode

from . import services
from .models import Employee, OrgUnit, PayrollResult, PayrollRun, Position

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


def _code(obj):
    return obj.code if obj is not None else None


def _cost_center(company_code, code):
    if not code:
        return None
    cost_center = CostCenter.objects.filter(company_code=company_code, code=code).first()
    if cost_center is None:
        raise serializers.ValidationError({"cost_center": f"Cost centre {code} not found in {company_code.code}."})
    return cost_center


class OrgUnitSerializer(serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    parent = serializers.PrimaryKeyRelatedField(queryset=OrgUnit.objects.all(), required=False, allow_null=True)
    cost_center = serializers.CharField(source="cost_center.code", required=False, allow_null=True, default=None)

    class Meta:
        model = OrgUnit
        fields = ["id", "company_code", "code", "name", "parent", "cost_center", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def to_representation(self, instance):
        # Set explicitly: DRF drops a dotted-source field whose parent is empty on PATCH responses.
        return {**super().to_representation(instance), "cost_center": _code(instance.cost_center)}

    def validate(self, attrs):
        company_code = attrs.get("company_code", getattr(self.instance, "company_code", None))
        if "cost_center" in attrs:
            raw = attrs.pop("cost_center")
            attrs["cost_center"] = _cost_center(company_code, raw.get("code") if isinstance(raw, dict) else None)
        parent = attrs.get("parent")
        if parent is not None:
            if parent.company_code_id != company_code.pk:
                raise serializers.ValidationError({"parent": "The parent unit is in another company code."})
            node = parent
            while node is not None:  # no cycles
                if self.instance and node.pk == self.instance.pk:
                    raise serializers.ValidationError({"parent": "A unit cannot sit below itself."})
                node = node.parent
        return attrs


class PositionSerializer(serializers.ModelSerializer):
    org_unit = serializers.PrimaryKeyRelatedField(queryset=OrgUnit.objects.select_related("company_code"))
    cost_center = serializers.CharField(source="cost_center.code", required=False, allow_null=True, default=None)
    effective_cost_center = serializers.CharField(source="effective_cost_center.code", default=None,
                                                  read_only=True)

    class Meta:
        model = Position
        fields = ["id", "org_unit", "code", "title", "cost_center", "effective_cost_center", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def to_representation(self, instance):
        return {**super().to_representation(instance), "cost_center": _code(instance.cost_center),
                "effective_cost_center": _code(instance.effective_cost_center)}

    def validate(self, attrs):
        org_unit = attrs.get("org_unit", getattr(self.instance, "org_unit", None))
        if "cost_center" in attrs:
            raw = attrs.pop("cost_center")
            attrs["cost_center"] = _cost_center(org_unit.company_code,
                                                raw.get("code") if isinstance(raw, dict) else None)
        return attrs


class EmployeeSerializer(serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    position = serializers.PrimaryKeyRelatedField(queryset=Position.objects.select_related("org_unit"))
    monthly_salary = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0)
    cost_center = serializers.CharField(source="position.effective_cost_center.code", default=None, read_only=True)

    class Meta:
        model = Employee
        fields = ["id", "company_code", "personnel_number", "first_name", "last_name", "email", "position",
                  "cost_center", "hire_date", "termination_date", "monthly_salary", "tax_reference",
                  *AUDIT_FIELDS]
        read_only_fields = ["id", "personnel_number", *AUDIT_FIELDS]

    def to_representation(self, instance):
        return {**super().to_representation(instance), "cost_center": _code(instance.position.effective_cost_center)}

    def validate(self, attrs):
        company_code = attrs.get("company_code", getattr(self.instance, "company_code", None))
        position = attrs.get("position", getattr(self.instance, "position", None))
        if position.org_unit.company_code_id != company_code.pk:
            raise serializers.ValidationError({"position": f"Position {position.code} is not in company code "
                                                           f"{company_code.code}."})
        hire = attrs.get("hire_date", getattr(self.instance, "hire_date", None))
        leave = attrs.get("termination_date", getattr(self.instance, "termination_date", None))
        if leave is not None and leave < hire:
            raise serializers.ValidationError({"termination_date": "Cannot be before the hire date."})
        return attrs

    def create(self, validated_data):
        user = validated_data.pop("created_by", None)
        validated_data.pop("updated_by", None)
        return services.create_employee(user=user, **validated_data)


class PayrollResultSerializer(serializers.ModelSerializer):
    personnel_number = serializers.CharField(source="employee.personnel_number")
    name = serializers.CharField(source="employee.full_name")
    cost_center = serializers.CharField(source="cost_center.code")

    class Meta:
        model = PayrollResult
        fields = ["personnel_number", "name", "cost_center", "gross", "paye", "uif_employee", "uif_employer",
                  "sdl", "net"]
        read_only_fields = fields


class PayrollRunSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    journal_entry = serializers.CharField(source="journal_entry.document_number")
    results = PayrollResultSerializer(many=True)

    class Meta:
        model = PayrollRun
        fields = ["id", "run_number", "company_code", "year", "month", "period_start", "period_end", "posting_date",
                  "tax_year", "gross", "paye", "uif_employee", "uif_employer", "sdl", "net", "status",
                  "journal_entry", "results", "created_at", "created_by"]
        read_only_fields = fields


class PayrollRunCreateSerializer(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    posting_date = serializers.DateField(required=False)

    def post(self, user) -> PayrollRun:
        data = self.validated_data
        try:
            return services.post_payroll_run(data["company_code"], year=data["year"], month=data["month"],
                                             posting_date=data.get("posting_date"), user=user)
        except PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


class PostingDateSerializer(serializers.Serializer):
    posting_date = serializers.DateField(required=False)
