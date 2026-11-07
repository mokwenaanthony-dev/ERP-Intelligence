"""Serializers for goods movements and moving-average stock valuation: validate API input and shape API output.

Input serializers check types and required fields only; business rules live in ``services.py``.
Output serializers are read-only views of posted documents.
"""
from rest_framework import serializers

from apps.finance.models import ChartOfAccounts, GLAccount
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import Plant, StorageLocation

from . import services
from .models import InventoryAccountSetting, MaterialDocument, MaterialDocumentItem, MovementType


AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


class GoodsMovementItemInputSerializer(serializers.Serializer):
    material = serializers.CharField()
    storage_location = serializers.CharField(max_length=4)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, required=False, allow_null=True)
    to_storage_location = serializers.CharField(max_length=4, required=False, allow_blank=True)
    text = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Must be greater than zero.")
        return value

    def validate_unit_price(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Must not be negative.")
        return value


class GoodsMovementCreateSerializer(serializers.Serializer):
    """Input for posting a goods movement. Materials, plants and storage locations are given by their codes."""

    movement_type = serializers.ChoiceField(choices=MovementType.choices)
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    posting_date = serializers.DateField()
    document_date = serializers.DateField(required=False)
    reference = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    header_text = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    business_partner = serializers.SlugRelatedField(
        slug_field="number", queryset=BusinessPartner.objects.all(), required=False, allow_null=True
    )
    items = GoodsMovementItemInputSerializer(many=True)

    def validate(self, attrs):
        plant = attrs["plant"]
        locations = {location.code: location for location in StorageLocation.objects.filter(plant=plant)}
        item_inputs, item_errors = [], []
        for raw in attrs["items"]:
            errors = {}
            material = Material.objects.select_related("base_unit").filter(number=raw["material"]).first()
            if material is None:
                errors["material"] = f"Material {raw['material']} not found."
            location = locations.get(raw["storage_location"])
            if location is None:
                errors["storage_location"] = (
                    f"Storage location {raw['storage_location']} not found in plant {plant.code}."
                )
            target = None
            if raw.get("to_storage_location"):
                target = locations.get(raw["to_storage_location"])
                if target is None:
                    errors["to_storage_location"] = (
                        f"Storage location {raw['to_storage_location']} not found in plant {plant.code}."
                    )
            item_errors.append(errors)
            if not errors:
                item_inputs.append(services.ItemInput(
                    material=material, storage_location=location, quantity=raw["quantity"],
                    unit_price=raw.get("unit_price"), to_storage_location=target, text=raw.get("text", ""),
                ))
        if any(item_errors):
            raise serializers.ValidationError({"items": item_errors})
        attrs["item_inputs"] = item_inputs
        return attrs

    def post(self, user) -> MaterialDocument:
        data = self.validated_data
        try:
            return services.post_goods_movement(
                movement_type=data["movement_type"], plant=data["plant"], posting_date=data["posting_date"],
                document_date=data.get("document_date"), reference=data["reference"],
                header_text=data["header_text"], business_partner=data.get("business_partner"),
                items=data["item_inputs"], user=user,
            )
        except services.PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


class MaterialDocumentItemSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    material_description = serializers.CharField(source="material.description")
    storage_location = serializers.CharField(source="storage_location.code")
    unit = serializers.CharField(source="unit.code")

    class Meta:
        model = MaterialDocumentItem
        fields = ["line_number", "material", "material_description", "storage_location", "quantity", "unit",
                  "amount_local", "text"]
        read_only_fields = fields


class MaterialDocumentSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    plant = serializers.CharField(source="plant.code")
    movement_type_name = serializers.CharField(source="get_movement_type_display")
    business_partner = serializers.CharField(source="business_partner.number", default=None)
    journal_entry = serializers.CharField(source="journal_entry.document_number", default=None)
    reversal_of = serializers.CharField(source="reversal_of.document_number", default=None)
    reversed_by = serializers.SerializerMethodField()
    items = MaterialDocumentItemSerializer(many=True)

    class Meta:
        model = MaterialDocument
        fields = ["id", "company_code", "plant", "fiscal_year", "document_number", "movement_type",
                  "movement_type_name", "posting_date", "document_date", "reference", "header_text",
                  "business_partner", "journal_entry", "status", "reversal_of", "reversed_by", "items",
                  "created_at", "created_by"]
        read_only_fields = fields

    def get_reversed_by(self, document):
        reversal = getattr(document, "reversed_by", None)  # the reverse one-to-one raises when absent
        return reversal.document_number if reversal else None


class InventoryAccountSettingSerializer(serializers.ModelSerializer):
    chart_of_accounts = serializers.SlugRelatedField(slug_field="code", queryset=ChartOfAccounts.objects.all())
    gl_account = serializers.CharField(source="gl_account.number")

    class Meta:
        model = InventoryAccountSetting
        fields = ["id", "chart_of_accounts", "account_key", "material_type", "gl_account", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        chart = attrs.get("chart_of_accounts", getattr(self.instance, "chart_of_accounts", None))
        account_data = attrs.pop("gl_account", None)
        if account_data:
            number = account_data["number"]
            account = GLAccount.objects.filter(chart_of_accounts=chart, number=number).first()
            if account is None:
                raise serializers.ValidationError({"gl_account": f"Account {number} is not in chart {chart.code}."})
            attrs["gl_account"] = account
        material_type = attrs.get("material_type", "")
        if material_type and material_type not in Material.MaterialType.values:
            raise serializers.ValidationError({"material_type": f"Unknown material type {material_type}."})
        return attrs
