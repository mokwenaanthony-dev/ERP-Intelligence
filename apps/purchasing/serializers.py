"""Serializers for procure-to-pay: validate API input and shape API output.

Input serializers check types and required fields only; business rules live in ``services.py``.
Output serializers are read-only views of posted documents.
"""
from rest_framework import serializers

from apps.finance.models import TaxCode
from apps.finance.services import PostingError
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import Plant, PurchasingOrganization, StorageLocation

from . import services
from .models import GoodsReceipt, PurchaseOrder, PurchaseOrderItem, SupplierInvoice, SupplierInvoiceItem

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


def _rule_error(exc):
    return serializers.ValidationError({"non_field_errors": [str(exc)]})


def _positive(value):
    if value <= 0:
        raise serializers.ValidationError("Must be greater than zero.")
    return value


# --- Purchase orders ----------------------------------------------------------------------

class OrderItemInputSerializer(serializers.Serializer):
    material = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.select_related("base_unit"))
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3, validators=[_positive])
    net_price = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0)
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.all(), required=False)
    storage_location = serializers.CharField(max_length=4, required=False)
    tax_code = serializers.CharField(max_length=4, required=False)


def _item_inputs(raw_items, company_code):
    """Resolve tax codes and storage locations (both depend on the company code and plant)."""
    inputs, errors = [], []
    default_plant = Plant.objects.filter(company_code=company_code).order_by("code").first()
    for raw in raw_items:
        error, tax_code, location = {}, None, None
        if raw.get("tax_code"):
            tax_code = TaxCode.objects.filter(
                chart_of_accounts=company_code.chart_of_accounts, code=raw["tax_code"]
            ).first()
            if tax_code is None:
                error["tax_code"] = f"Tax code {raw['tax_code']} not found."
        if raw.get("storage_location"):
            plant = raw.get("plant") or default_plant
            location = StorageLocation.objects.filter(plant=plant, code=raw["storage_location"]).first()
            if location is None:
                error["storage_location"] = f"Storage location {raw['storage_location']} not found in the plant."
        errors.append(error)
        inputs.append(services.OrderItemInput(
            material=raw["material"], quantity=raw["quantity"], net_price=raw["net_price"], plant=raw.get("plant"),
            storage_location=location, tax_code=tax_code,
        ))
    if any(errors):
        raise serializers.ValidationError({"items": errors})
    return inputs


class PurchaseOrderCreateSerializer(serializers.Serializer):
    purchasing_organization = serializers.SlugRelatedField(
        slug_field="code", queryset=PurchasingOrganization.objects.select_related("company_code__chart_of_accounts")
    )
    supplier = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    order_date = serializers.DateField()
    delivery_date = serializers.DateField(required=False, allow_null=True)
    supplier_reference = serializers.CharField(max_length=35, required=False, allow_blank=True, default="")
    items = OrderItemInputSerializer(many=True)

    def validate(self, attrs):
        attrs["item_inputs"] = _item_inputs(attrs["items"], attrs["purchasing_organization"].company_code)
        return attrs

    def post(self, user) -> PurchaseOrder:
        data = self.validated_data
        try:
            return services.create_purchase_order(
                purchasing_organization=data["purchasing_organization"], supplier=data["supplier"],
                order_date=data["order_date"], delivery_date=data.get("delivery_date"),
                supplier_reference=data["supplier_reference"], items=data["item_inputs"], user=user,
            )
        except PostingError as exc:
            raise _rule_error(exc) from exc


class PurchaseOrderItemsUpdateSerializer(serializers.Serializer):
    items = OrderItemInputSerializer(many=True)


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    description = serializers.CharField(source="material.description")
    plant = serializers.CharField(source="plant.code")
    storage_location = serializers.CharField(source="storage_location.code")
    unit = serializers.CharField(source="unit.code")
    tax_code = serializers.CharField(source="tax_code.code")
    net_amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    received_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    invoiced_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    open_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        model = PurchaseOrderItem
        fields = ["item_number", "material", "description", "plant", "storage_location", "quantity", "unit",
                  "net_price", "net_amount", "tax_code", "received_quantity", "invoiced_quantity", "open_quantity"]
        read_only_fields = fields


class PurchaseOrderSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    purchasing_organization = serializers.CharField(source="purchasing_organization.code")
    supplier = serializers.CharField(source="supplier.number")
    supplier_name = serializers.CharField(source="supplier.name")
    net_total = serializers.DecimalField(max_digits=18, decimal_places=2)
    items = PurchaseOrderItemSerializer(many=True)
    receipts = serializers.SlugRelatedField(slug_field="receipt_number", many=True, read_only=True)
    invoices = serializers.SlugRelatedField(slug_field="invoice_number", many=True, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ["id", "order_number", "company_code", "purchasing_organization", "supplier", "supplier_name",
                  "order_date", "delivery_date", "supplier_reference", "status", "net_total", "items", "receipts",
                  "invoices", *AUDIT_FIELDS]
        read_only_fields = fields


# --- Goods receipts --------------------------------------------------------------------------

class ReceiveItemSerializer(serializers.Serializer):
    item_number = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    storage_location = serializers.CharField(max_length=4, required=False)


class ReceiveSerializer(serializers.Serializer):
    """Receive selected items, or everything still open into each item's storage location."""

    purchase_order = serializers.SlugRelatedField(slug_field="order_number", queryset=PurchaseOrder.objects.all())
    receipt_date = serializers.DateField()
    delivery_note = serializers.CharField(max_length=35, required=False, allow_blank=True, default="")
    items = ReceiveItemSerializer(many=True, required=False)

    def post(self, user) -> GoodsReceipt:
        data = self.validated_data
        order = data["purchase_order"]
        items = None
        if data.get("items") is not None:
            order_items = {item.item_number: item for item in order.items.all()}
            items, errors = [], []
            for raw in data["items"]:
                order_item = order_items.get(raw["item_number"])
                if order_item is None:
                    errors.append({"item_number": "Not found on this order."})
                    continue
                location = None
                if raw.get("storage_location"):
                    location = StorageLocation.objects.filter(plant=order_item.plant,
                                                              code=raw["storage_location"]).first()
                    if location is None:
                        errors.append({"storage_location": "Not found in the item's plant."})
                        continue
                errors.append({})
                items.append(services.ReceiptItemInput(order_item, raw["quantity"], location))
            if any(errors):
                raise serializers.ValidationError({"items": errors})
        try:
            return services.post_goods_receipt(order, receipt_date=data["receipt_date"], items=items,
                                               delivery_note=data["delivery_note"], user=user)
        except PostingError as exc:
            raise _rule_error(exc) from exc


class GoodsReceiptItemSerializer(serializers.Serializer):
    item_number = serializers.IntegerField(source="order_item.item_number")
    material = serializers.CharField(source="order_item.material.number")
    storage_location = serializers.CharField(source="storage_location.code")
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    value = serializers.DecimalField(max_digits=18, decimal_places=2)


class GoodsReceiptSerializer(serializers.ModelSerializer):
    purchase_order = serializers.CharField(source="purchase_order.order_number")
    supplier = serializers.CharField(source="purchase_order.supplier.number")
    plant = serializers.CharField(source="plant.code")
    material_document = serializers.CharField(source="material_document.document_number")
    journal_entry = serializers.CharField(source="material_document.journal_entry.document_number", default=None)
    items = GoodsReceiptItemSerializer(many=True)

    class Meta:
        model = GoodsReceipt
        fields = ["id", "receipt_number", "purchase_order", "supplier", "plant", "receipt_date", "delivery_note",
                  "status", "material_document", "journal_entry", "items", "created_at", "created_by"]
        read_only_fields = fields


# --- Supplier invoices -----------------------------------------------------------------------

class InvoiceItemInputSerializer(serializers.Serializer):
    item_number = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    net_price = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)


class SupplierInvoiceCreateSerializer(serializers.Serializer):
    purchase_order = serializers.SlugRelatedField(slug_field="order_number", queryset=PurchaseOrder.objects.all())
    supplier_reference = serializers.CharField(max_length=35)
    invoice_date = serializers.DateField()
    items = InvoiceItemInputSerializer(many=True, required=False)

    def post(self, user) -> SupplierInvoice:
        data = self.validated_data
        order = data["purchase_order"]
        items = None
        if data.get("items") is not None:
            order_items = {item.item_number: item for item in order.items.all()}
            errors = [{} if raw["item_number"] in order_items else {"item_number": "Not found on this order."}
                      for raw in data["items"]]
            if any(errors):
                raise serializers.ValidationError({"items": errors})
            items = [services.InvoiceItemInput(order_items[raw["item_number"]], raw["quantity"], raw.get("net_price"))
                     for raw in data["items"]]
        try:
            return services.post_supplier_invoice(order, invoice_date=data["invoice_date"], items=items,
                                                  supplier_reference=data["supplier_reference"], user=user)
        except PostingError as exc:
            raise _rule_error(exc) from exc


class SupplierInvoiceItemSerializer(serializers.ModelSerializer):
    item_number = serializers.IntegerField(source="order_item.item_number")
    material = serializers.CharField(source="order_item.material.number")
    order_price = serializers.DecimalField(source="order_item.net_price", max_digits=18, decimal_places=2)
    tax_code = serializers.CharField(source="tax_code.code")

    class Meta:
        model = SupplierInvoiceItem
        fields = ["item_number", "material", "quantity", "order_price", "net_price", "net_amount", "gr_ir_amount",
                  "price_difference", "tax_code", "tax_amount"]
        read_only_fields = fields


class SupplierInvoiceSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    supplier = serializers.CharField(source="supplier.number")
    supplier_name = serializers.CharField(source="supplier.name")
    purchase_order = serializers.CharField(source="purchase_order.order_number")
    journal_entry = serializers.CharField(source="journal_entry.document_number")
    open_amount = serializers.SerializerMethodField()
    items = SupplierInvoiceItemSerializer(many=True)

    class Meta:
        model = SupplierInvoice
        fields = ["id", "invoice_number", "supplier_reference", "company_code", "supplier", "supplier_name",
                  "purchase_order", "invoice_date", "due_date", "net_amount", "tax_amount", "gross_amount",
                  "open_amount", "status", "journal_entry", "items", "created_at", "created_by"]
        read_only_fields = fields

    def get_open_amount(self, invoice):
        return str(services.supplier_invoice_open_amount(invoice))


class PostingDateSerializer(serializers.Serializer):
    posting_date = serializers.DateField(required=False)
