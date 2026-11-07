"""Serializers for order-to-cash: validate API input and shape API output.

Input serializers check types and required fields only; business rules live in ``services.py``.
Output serializers are read-only views of posted documents.
"""
from rest_framework import serializers

from apps.finance.models import TaxCode
from apps.finance.services import PostingError
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import Plant, SalesOrganization, StorageLocation

from . import services
from .models import Delivery, Invoice, InvoiceItem, SalesOrder, SalesOrderItem, SalesPrice

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


def _rule_error(exc):
    return serializers.ValidationError({"non_field_errors": [str(exc)]})


# --- Prices ---------------------------------------------------------------------------------

class SalesPriceSerializer(serializers.ModelSerializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all())
    sales_organization = serializers.SlugRelatedField(slug_field="code", queryset=SalesOrganization.objects.all())
    price = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0)

    class Meta:
        model = SalesPrice
        fields = ["id", "material", "sales_organization", "price", "valid_from", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


# --- Sales orders ---------------------------------------------------------------------------

class OrderItemInputSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.select_related("base_unit"))
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.all(), required=False)
    net_price = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)
    tax_code = serializers.CharField(max_length=4, required=False)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Must be greater than zero.")
        return value


def _item_inputs(raw_items, company_code):
    inputs, errors = [], []
    for raw in raw_items:
        tax_code = None
        if raw.get("tax_code"):
            tax_code = TaxCode.objects.filter(
                chart_of_accounts=company_code.chart_of_accounts, code=raw["tax_code"]
            ).first()
            if tax_code is None:
                errors.append({"tax_code": f"Tax code {raw['tax_code']} not found."})
                continue
        errors.append({})
        inputs.append(services.OrderItemInput(
            material=raw["material"], quantity=raw["quantity"], plant=raw.get("plant"),
            net_price=raw.get("net_price"), tax_code=tax_code,
        ))
    if any(errors):
        raise serializers.ValidationError({"items": errors})
    return inputs


class SalesOrderCreateSerializer(serializers.Serializer):
    sales_organization = serializers.SlugRelatedField(
        slug_field="code", queryset=SalesOrganization.objects.select_related("company_code__chart_of_accounts")
    )
    customer = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    order_date = serializers.DateField()
    requested_delivery_date = serializers.DateField(required=False, allow_null=True)
    customer_reference = serializers.CharField(max_length=35, required=False, allow_blank=True, default="")
    items = OrderItemInputSerializer(many=True)

    def validate(self, attrs):
        attrs["item_inputs"] = _item_inputs(attrs["items"], attrs["sales_organization"].company_code)
        return attrs

    def post(self, user) -> SalesOrder:
        data = self.validated_data
        try:
            return services.create_sales_order(
                sales_organization=data["sales_organization"], customer=data["customer"],
                order_date=data["order_date"], requested_delivery_date=data.get("requested_delivery_date"),
                customer_reference=data["customer_reference"], items=data["item_inputs"], user=user,
            )
        except PostingError as exc:
            raise _rule_error(exc) from exc


class SalesOrderItemsUpdateSerializer(serializers.Serializer):
    items = OrderItemInputSerializer(many=True)


class SalesOrderItemSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    description = serializers.CharField(source="material.description")
    plant = serializers.CharField(source="plant.code")
    unit = serializers.CharField(source="unit.code")
    tax_code = serializers.CharField(source="tax_code.code")
    net_amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    delivered_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    invoiced_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    open_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        model = SalesOrderItem
        fields = ["item_number", "material", "description", "plant", "quantity", "unit", "net_price",
                  "price_overridden", "net_amount", "tax_code", "delivered_quantity", "invoiced_quantity",
                  "open_quantity"]
        read_only_fields = fields


class SalesOrderSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    sales_organization = serializers.CharField(source="sales_organization.code")
    customer = serializers.CharField(source="customer.number")
    customer_name = serializers.CharField(source="customer.name")
    net_total = serializers.DecimalField(max_digits=18, decimal_places=2)
    items = SalesOrderItemSerializer(many=True)
    deliveries = serializers.SlugRelatedField(slug_field="delivery_number", many=True, read_only=True)

    class Meta:
        model = SalesOrder
        fields = ["id", "order_number", "company_code", "sales_organization", "customer", "customer_name",
                  "order_date", "requested_delivery_date", "customer_reference", "status", "net_total", "items",
                  "deliveries", *AUDIT_FIELDS]
        read_only_fields = fields


class DeliverItemSerializer(serializers.Serializer):
    item_number = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    storage_location = serializers.CharField(max_length=4)


class DeliverSerializer(serializers.Serializer):
    """Deliver selected items, or everything still open from one storage location."""

    delivery_date = serializers.DateField()
    storage_location = serializers.CharField(max_length=4, required=False)
    items = DeliverItemSerializer(many=True, required=False)

    def post(self, order: SalesOrder, user) -> Delivery:
        data = self.validated_data
        plant_ids = {item.plant_id for item in order.items.all()}
        locations = {(loc.plant_id, loc.code): loc for loc in StorageLocation.objects.filter(plant_id__in=plant_ids)}
        order_items = {item.item_number: item for item in order.items.all()}
        items = default_location = None
        if data.get("items") is not None:
            items, errors = [], []
            for raw in data["items"]:
                order_item = order_items.get(raw["item_number"])
                location = locations.get((order_item.plant_id, raw["storage_location"])) if order_item else None
                errors.append({} if order_item and location else {
                    "item_number" if not order_item else "storage_location": "Not found on this order/plant."
                })
                if order_item and location:
                    items.append(services.DeliveryItemInput(order_item, raw["quantity"], location))
            if any(errors):
                raise serializers.ValidationError({"items": errors})
        elif data.get("storage_location"):
            first = next(iter(order_items.values()), None)
            default_location = locations.get((first.plant_id, data["storage_location"])) if first else None
            if default_location is None:
                raise serializers.ValidationError({"storage_location": "Storage location not found in the plant."})
        try:
            return services.post_delivery(order, delivery_date=data["delivery_date"], items=items,
                                          storage_location=default_location, user=user)
        except PostingError as exc:
            raise _rule_error(exc) from exc


# --- Deliveries and invoices -------------------------------------------------------------------

class DeliveryItemSerializer(serializers.Serializer):
    item_number = serializers.IntegerField(source="order_item.item_number")
    material = serializers.CharField(source="order_item.material.number")
    storage_location = serializers.CharField(source="storage_location.code")
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)


class DeliverySerializer(serializers.ModelSerializer):
    sales_order = serializers.CharField(source="sales_order.order_number")
    customer = serializers.CharField(source="sales_order.customer.number")
    plant = serializers.CharField(source="plant.code")
    material_document = serializers.CharField(source="material_document.document_number")
    goods_issue_journal_entry = serializers.CharField(source="material_document.journal_entry.document_number",
                                                      default=None)
    invoices = serializers.SlugRelatedField(slug_field="invoice_number", many=True, read_only=True)
    items = DeliveryItemSerializer(many=True)

    class Meta:
        model = Delivery
        fields = ["id", "delivery_number", "sales_order", "customer", "plant", "delivery_date", "status",
                  "material_document", "goods_issue_journal_entry", "invoices", "items", "created_at", "created_by"]
        read_only_fields = fields


class InvoiceCreateSerializer(serializers.Serializer):
    delivery = serializers.PrimaryKeyRelatedField(queryset=Delivery.objects.all())
    invoice_date = serializers.DateField()

    def post(self, user) -> Invoice:
        try:
            return services.post_invoice(self.validated_data["delivery"],
                                         invoice_date=self.validated_data["invoice_date"], user=user)
        except PostingError as exc:
            raise _rule_error(exc) from exc


class InvoiceItemSerializer(serializers.ModelSerializer):
    item_number = serializers.IntegerField(source="order_item.item_number")
    material = serializers.CharField(source="order_item.material.number")
    tax_code = serializers.CharField(source="tax_code.code")

    class Meta:
        model = InvoiceItem
        fields = ["item_number", "material", "quantity", "net_price", "net_amount", "tax_code", "tax_amount"]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    customer = serializers.CharField(source="customer.number")
    customer_name = serializers.CharField(source="customer.name")
    delivery = serializers.CharField(source="delivery.delivery_number")
    sales_order = serializers.CharField(source="delivery.sales_order.order_number")
    journal_entry = serializers.CharField(source="journal_entry.document_number")
    open_amount = serializers.SerializerMethodField()
    items = InvoiceItemSerializer(many=True)

    class Meta:
        model = Invoice
        fields = ["id", "invoice_number", "company_code", "customer", "customer_name", "delivery", "sales_order",
                  "invoice_date", "due_date", "net_amount", "tax_amount", "gross_amount", "open_amount", "status",
                  "journal_entry", "items", "created_at", "created_by"]
        read_only_fields = fields

    def get_open_amount(self, invoice):
        return str(services.invoice_open_amount(invoice))


class PostingDateSerializer(serializers.Serializer):
    posting_date = serializers.DateField(required=False)
