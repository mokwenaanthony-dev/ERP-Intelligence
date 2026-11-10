"""Serializers for production planning: validate API input and shape API output.

Business rules (unique codes per plant, cost centre checks, BOM cycles) are in ``services.py``; the
serializers call them and turn a ``ProductionError`` into a 400. Equipment codes cannot be changed
after creation because they are graph keys and Unified Namespace topic segments.
"""
from decimal import Decimal

from rest_framework import serializers

from apps.controlling.models import CostCenter
from apps.finance.models import GLAccount
from apps.masterdata.models import Material, UnitOfMeasure
from apps.organization.models import CompanyCode, Plant

from . import orders, services
from .models import (
    ActivityRate,
    ActivityType,
    BillOfMaterials,
    Confirmation,
    ProductionArea,
    ProductionLine,
    ProductionOrder,
    Routing,
    WorkCenter,
)

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


def rule_error(exc):
    """A broken business rule as a DRF 400. Raise it ``from exc`` so the cause (and its line) is logged."""
    return serializers.ValidationError({"non_field_errors": [str(exc)]})


class _RuleCheckedMixin:
    """Run the service-layer rules when the record is saved, not in ``validate()``.

    DRF re-raises errors from ``validate()`` as new exceptions, which drops the original
    ``ProductionError`` and with it the file and line the exception handler logs. Raised from
    ``save()`` with ``from exc``, the cause survives.
    """

    def check_rules(self, data):
        """Override: raise ``ProductionError`` if the record would break a rule."""

    def _final(self, data, name):
        return data[name] if name in data else getattr(self.instance, name, None)

    def save(self, **kwargs):
        try:
            self.check_rules({**self.validated_data, **kwargs})
        except services.ProductionError as exc:
            raise rule_error(exc) from exc
        return super().save(**kwargs)


class _FixedCodeMixin:
    """Codes are keys in the graph and topic segments in the UNS: set once, never changed."""

    def validate_code(self, value):
        if self.instance is not None and value != self.instance.code:
            raise serializers.ValidationError(
                "Codes cannot be changed after creation: they are used as graph keys and UNS topic paths."
            )
        return value


# --- equipment hierarchy ---------------------------------------------------------------------

class ProductionAreaSerializer(_FixedCodeMixin, serializers.ModelSerializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    uns_path = serializers.CharField(read_only=True)
    isa95_level = serializers.CharField(read_only=True)

    class Meta:
        model = ProductionArea
        fields = ["id", "plant", "code", "name", "is_active", "isa95_level", "uns_path", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate_plant(self, value):
        if self.instance is not None and value != self.instance.plant:
            raise serializers.ValidationError("An area cannot move to another plant.")
        return value


class ProductionLineSerializer(_RuleCheckedMixin, _FixedCodeMixin, serializers.ModelSerializer):
    area = serializers.PrimaryKeyRelatedField(queryset=ProductionArea.objects.select_related("plant__company_code"))
    plant = serializers.CharField(source="area.plant.code", read_only=True)
    uns_path = serializers.CharField(read_only=True)
    isa95_level = serializers.CharField(read_only=True)
    isa95_type = serializers.CharField(read_only=True)

    class Meta:
        model = ProductionLine
        fields = ["id", "area", "plant", "code", "name", "is_active", "isa95_level", "isa95_type", "uns_path",
                  *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        area = attrs.get("area", getattr(self.instance, "area", None))
        if self.instance is not None and area != self.instance.area:
            raise serializers.ValidationError({"area": "A line cannot move to another area."})
        return attrs

    def check_rules(self, data):
        services.check_line_code(self._final(data, "area"), self._final(data, "code"),
                                 exclude_pk=getattr(self.instance, "pk", None))


class WorkCenterSerializer(_RuleCheckedMixin, _FixedCodeMixin, serializers.ModelSerializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    line = serializers.PrimaryKeyRelatedField(queryset=ProductionLine.objects.select_related("area__plant"))
    cost_center = serializers.CharField(source="cost_center.code")
    activity_types = serializers.SlugRelatedField(slug_field="code", many=True, required=False,
                                                  queryset=ActivityType.objects.all())
    uns_path = serializers.CharField(read_only=True)

    class Meta:
        model = WorkCenter
        fields = ["id", "plant", "line", "code", "name", "isa95_level", "cost_center", "activity_types",
                  "capacity_hours_per_day", "is_active", "uns_path", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        plant = attrs.get("plant", getattr(self.instance, "plant", None))
        if self.instance is not None and plant != self.instance.plant:
            raise serializers.ValidationError({"plant": "A work centre cannot move to another plant."})
        line = attrs.get("line", getattr(self.instance, "line", None))
        if "cost_center" in attrs:
            code = attrs.pop("cost_center")["code"]
            cost_center = CostCenter.objects.filter(company_code=plant.company_code, code=code).first()
            if cost_center is None:
                raise serializers.ValidationError(
                    {"cost_center": f"Cost centre {code} not found in company code {plant.company_code.code}."}
                )
            attrs["cost_center"] = cost_center
        if line is None:
            raise serializers.ValidationError({"line": "Required."})
        return attrs

    def check_rules(self, data):
        activity_types = data.get("activity_types")
        if activity_types is None and self.instance is not None:
            activity_types = list(self.instance.activity_types.all())
        services.check_work_center(self._final(data, "plant"), self._final(data, "line"),
                                   self._final(data, "cost_center"), activity_types or [])


# --- costing ---------------------------------------------------------------------------------

class ActivityTypeSerializer(_FixedCodeMixin, serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    unit = serializers.SlugRelatedField(slug_field="code", queryset=UnitOfMeasure.objects.all())
    allocation_account = serializers.CharField(source="allocation_account.number")

    class Meta:
        model = ActivityType
        fields = ["id", "company_code", "code", "name", "unit", "allocation_account", "is_active", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        company_code = attrs.get("company_code", getattr(self.instance, "company_code", None))
        if self.instance is not None and company_code != self.instance.company_code:
            raise serializers.ValidationError({"company_code": "An activity type cannot move to another company."})
        if "allocation_account" in attrs:
            number = attrs.pop("allocation_account")["number"]
            account = GLAccount.objects.filter(chart_of_accounts=company_code.chart_of_accounts, number=number).first()
            if account is None:
                raise serializers.ValidationError({"allocation_account": f"G/L account {number} not found."})
            # The allocation credits a cost centre, so it must be an expense account like the costs it relieves.
            if account.account_type != GLAccount.AccountType.EXPENSE:
                raise serializers.ValidationError({"allocation_account": "Must be an expense account."})
            attrs["allocation_account"] = account
        return attrs


class ActivityRateSerializer(serializers.ModelSerializer):
    cost_center = serializers.PrimaryKeyRelatedField(queryset=CostCenter.objects.select_related("company_code"))
    activity_type = serializers.PrimaryKeyRelatedField(queryset=ActivityType.objects.all())
    cost_center_code = serializers.CharField(source="cost_center.code", read_only=True)
    activity_type_code = serializers.CharField(source="activity_type.code", read_only=True)

    class Meta:
        model = ActivityRate
        fields = ["id", "cost_center", "cost_center_code", "activity_type", "activity_type_code", "valid_from",
                  "price", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        cost_center = attrs.get("cost_center", getattr(self.instance, "cost_center", None))
        activity_type = attrs.get("activity_type", getattr(self.instance, "activity_type", None))
        if cost_center.company_code_id != activity_type.company_code_id:
            raise serializers.ValidationError("Cost centre and activity type must be in the same company code.")
        return attrs


# --- bills of materials ----------------------------------------------------------------------

class BOMItemInputSerializer(serializers.Serializer):
    component = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3)
    item_number = serializers.IntegerField(min_value=1, required=False)


class BOMCreateSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    valid_from = serializers.DateField()
    base_quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=Decimal("0.001"),
                                             default=Decimal("1"))
    description = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    items = BOMItemInputSerializer(many=True)

    def post(self, user) -> BillOfMaterials:
        data = self.validated_data
        items = [services.ComponentInput(component=i["component"], quantity=i["quantity"],
                                         item_number=i.get("item_number")) for i in data["items"]]
        try:
            return services.create_bom(material=data["material"], plant=data["plant"], valid_from=data["valid_from"],
                                       items=items, base_quantity=data["base_quantity"],
                                       description=data["description"], user=user)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class BOMSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    plant = serializers.CharField(source="plant.code")
    items = serializers.SerializerMethodField()

    class Meta:
        model = BillOfMaterials
        fields = ["id", "key", "material", "plant", "valid_from", "base_quantity", "status", "description", "items",
                  *AUDIT_FIELDS]
        read_only_fields = fields

    def get_items(self, bom):
        return [{"item_number": i.item_number, "component": i.component.number,
                 "description": i.component.description, "quantity": str(i.quantity),
                 "unit": i.component.base_unit.code} for i in bom.items.all()]


# --- routings --------------------------------------------------------------------------------

class ActivityInputSerializer(serializers.Serializer):
    activity_type = serializers.CharField(max_length=6)
    setup_quantity = serializers.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantity_per_base = serializers.DecimalField(max_digits=12, decimal_places=3, default=0)


class OperationInputSerializer(serializers.Serializer):
    operation_number = serializers.IntegerField(min_value=1, required=False)
    description = serializers.CharField(max_length=100)
    work_center = serializers.CharField(max_length=10)
    activities = ActivityInputSerializer(many=True, required=False, default=list)


class RoutingCreateSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    valid_from = serializers.DateField()
    base_quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=Decimal("0.001"),
                                             default=Decimal("1"))
    description = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    operations = OperationInputSerializer(many=True)

    def validate(self, attrs):
        plant = attrs["plant"]
        operations = []
        for raw in attrs["operations"]:
            work_center = WorkCenter.objects.filter(plant=plant, code=raw["work_center"]).first()
            if work_center is None:
                raise serializers.ValidationError(
                    {"operations": f"Work centre {raw['work_center']} not found in plant {plant.code}."}
                )
            activities = []
            for act in raw["activities"]:
                activity_type = ActivityType.objects.filter(company_code=plant.company_code,
                                                            code=act["activity_type"]).first()
                if activity_type is None:
                    raise serializers.ValidationError(
                        {"operations": f"Activity type {act['activity_type']} not found."}
                    )
                activities.append(services.ActivityInput(activity_type, act["setup_quantity"],
                                                         act["quantity_per_base"]))
            operations.append(services.OperationInput(description=raw["description"], work_center=work_center,
                                                      activities=activities,
                                                      operation_number=raw.get("operation_number")))
        attrs["operation_inputs"] = operations
        return attrs

    def post(self, user) -> Routing:
        data = self.validated_data
        try:
            return services.create_routing(material=data["material"], plant=data["plant"],
                                           valid_from=data["valid_from"], operations=data["operation_inputs"],
                                           base_quantity=data["base_quantity"], description=data["description"],
                                           user=user)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class RoutingSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    plant = serializers.CharField(source="plant.code")
    operations = serializers.SerializerMethodField()

    class Meta:
        model = Routing
        fields = ["id", "key", "material", "plant", "valid_from", "base_quantity", "status", "description",
                  "operations", *AUDIT_FIELDS]
        read_only_fields = fields

    def get_operations(self, routing):
        return [{
            "operation_number": op.operation_number, "description": op.description,
            "work_center": op.work_center.code,
            "activities": [{"activity_type": a.activity_type.code, "setup_quantity": str(a.setup_quantity),
                            "quantity_per_base": str(a.quantity_per_base)} for a in op.activities.all()],
        } for op in routing.operations.all()]


class StatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=BillOfMaterials.Status.choices)


# --- Wave 5b: production orders ----------------------------------------------------------------
# Order actions raise rule errors in ``post()``, never in ``validate()``, so the logged cause keeps
# the file and line of the rule (see _RuleCheckedMixin).

class ProductionOrderCreateSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=Decimal("0.001"))
    start_date = serializers.DateField()
    finish_date = serializers.DateField(required=False, allow_null=True)
    receipt_storage_location = serializers.CharField(max_length=4, required=False)

    def post(self, user):
        data = self.validated_data
        location = None
        if data.get("receipt_storage_location"):
            location = data["plant"].storage_locations.filter(code=data["receipt_storage_location"]).first()
            if location is None:
                raise serializers.ValidationError({"receipt_storage_location": "Not found in this plant."})
        try:
            return orders.create_order(plant=data["plant"], material=data["material"], quantity=data["quantity"],
                                       start_date=data["start_date"], finish_date=data.get("finish_date"),
                                       receipt_storage_location=location, user=user)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class ProductionOrderSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    plant = serializers.CharField(source="plant.code")
    material = serializers.CharField(source="material.number")
    unit = serializers.CharField(source="unit.code")
    receipt_storage_location = serializers.CharField(source="receipt_storage_location.code")
    bom = serializers.CharField(source="bom.key")
    routing = serializers.CharField(source="routing.key", default=None)
    delivered_quantity = serializers.SerializerMethodField()
    settlement_document = serializers.CharField(source="settlement_entry.document_number", default=None)
    components = serializers.SerializerMethodField()
    operations = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrder
        fields = ["id", "company_code", "plant", "order_number", "material", "quantity", "unit",
                  "delivered_quantity", "receipt_storage_location", "start_date", "finish_date", "status",
                  "released_at", "completed_at", "bom", "routing", "planned_cost", "planned_unit_cost",
                  "settlement_document", "components", "operations", *AUDIT_FIELDS]
        read_only_fields = fields

    def get_delivered_quantity(self, order):
        return str(orders.delivered_quantity(order))

    def get_components(self, order):
        issued = orders.issued_quantities(order)
        return [{"item_number": c.item_number, "material": c.material.number,
                 "required_quantity": str(c.required_quantity),
                 "issued_quantity": str(issued.get(c.material_id, Decimal("0.000"))),
                 "storage_location": c.storage_location.code, "planned_value": str(c.planned_value)}
                for c in order.components.all()]

    def get_operations(self, order):
        return [{
            "operation_number": op.operation_number, "description": op.description,
            "work_center": op.work_center.code, "uns_path": op.work_center.uns_path,
            "finally_confirmed": any(c.is_final for c in op.confirmations.all()),
            "activities": [{"activity_type": a.activity_type.code, "planned_quantity": str(a.planned_quantity),
                            "planned_value": str(a.planned_value)} for a in op.activities.all()],
        } for op in order.operations.all()]


class PostingDateSerializer(serializers.Serializer):
    posting_date = serializers.DateField()


class IssueItemSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=Decimal("0.001"))
    storage_location = serializers.CharField(max_length=4, required=False)


class IssueSerializer(PostingDateSerializer):
    items = IssueItemSerializer(many=True, required=False)

    def post(self, order, user):
        data = self.validated_data
        items = None
        if data.get("items"):
            items = []
            for raw in data["items"]:
                location = None
                if raw.get("storage_location"):
                    location = order.plant.storage_locations.filter(code=raw["storage_location"]).first()
                    if location is None:
                        raise serializers.ValidationError({"items": f"Storage location {raw['storage_location']} "
                                                                    "not found in this plant."})
                items.append(orders.IssueInput(raw["material"], raw["quantity"], location))
        try:
            return orders.issue_components(order, data["posting_date"], items, user=user)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class ReceiveSerializer(PostingDateSerializer):
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=Decimal("0.001"))

    def post(self, order, user):
        try:
            return orders.receive_output(order, self.validated_data["posting_date"],
                                         self.validated_data["quantity"], user=user)
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class ConfirmSerializer(PostingDateSerializer):
    operation_number = serializers.IntegerField(min_value=1)
    yield_quantity = serializers.DecimalField(max_digits=15, decimal_places=3, default=Decimal("0"))
    scrap_quantity = serializers.DecimalField(max_digits=15, decimal_places=3, default=Decimal("0"))
    activities = serializers.DictField(child=serializers.DecimalField(max_digits=12, decimal_places=3),
                                       required=False, help_text='e.g. {"MACH": "10.5", "LAB": "10"}')
    final = serializers.BooleanField(default=False)
    external_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")

    def post(self, order, user):
        data = self.validated_data
        try:
            return orders.confirm_operation(
                order, data["operation_number"], data["posting_date"], yield_quantity=data["yield_quantity"],
                scrap_quantity=data["scrap_quantity"], activities=data.get("activities"), final=data["final"],
                external_id=data["external_id"], user=user,
            )
        except services.ProductionError as exc:
            raise rule_error(exc) from exc


class ConfirmationSerializer(serializers.ModelSerializer):
    order = serializers.CharField(source="order.order_number")
    operation_number = serializers.IntegerField(source="operation.operation_number")
    work_center = serializers.CharField(source="operation.work_center.code")
    journal_entry = serializers.CharField(source="journal_entry.document_number", default=None)
    activities = serializers.SerializerMethodField()

    class Meta:
        model = Confirmation
        fields = ["id", "order", "confirmation_number", "operation_number", "work_center", "posting_date",
                  "yield_quantity", "scrap_quantity", "is_final", "external_id", "journal_entry", "activities",
                  *AUDIT_FIELDS]
        read_only_fields = fields

    def get_activities(self, confirmation):
        return [{"activity_type": a.activity_type.code, "quantity": str(a.quantity), "rate": str(a.rate),
                 "amount": str(a.amount)} for a in confirmation.activities.all()]
