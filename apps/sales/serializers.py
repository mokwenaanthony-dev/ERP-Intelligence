from django.db import transaction
from rest_framework import serializers

from . import services
from .models import Customer, SalesOrder, SalesOrderLine


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id", "name", "email", "phone", "address",
            "created_at", "updated_at", "created_by", "updated_by",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "created_by", "updated_by"]


class SalesOrderLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    quantity = serializers.IntegerField(min_value=1)
    # Optional: when omitted the current catalogue price is captured; when
    # supplied it is a deliberate override (e.g. a negotiated discount).
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SalesOrderLine
        fields = ["id", "product", "product_name", "quantity", "unit_price", "line_total"]
        read_only_fields = ["id", "product_name", "line_total"]


class SalesOrderSerializer(serializers.ModelSerializer):
    lines = SalesOrderLineSerializer(many=True, required=False)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)

    class Meta:
        model = SalesOrder
        fields = [
            "id", "customer", "customer_name", "order_date", "status", "lines", "total",
            "created_at", "updated_at", "created_by", "updated_by",
        ]
        read_only_fields = ["id", "order_date", "created_at", "updated_at", "created_by", "updated_by"]

    def validate_status(self, value):
        if self.instance is None and value != SalesOrder.Status.PENDING:
            raise serializers.ValidationError("New orders start as PENDING; confirm them with an update.")
        return value

    def validate(self, attrs):
        order = self.instance
        if order is not None and not order.is_editable:
            locked = []
            if "lines" in attrs:
                locked.append("lines")
            if "customer" in attrs and attrs["customer"] != order.customer:
                locked.append("customer")
            if locked:
                raise serializers.ValidationError(
                    {field: f"Cannot change {field} once an order is {order.status}." for field in locked}
                )
        return attrs

    @staticmethod
    def _create_lines(order, lines_data, user):
        for line_data in lines_data:
            SalesOrderLine.objects.create(order=order, created_by=user, updated_by=user, **line_data)

    @transaction.atomic
    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        order = SalesOrder.objects.create(**validated_data)
        self._create_lines(order, lines_data, validated_data.get("created_by"))
        return order

    @transaction.atomic
    def update(self, instance, validated_data):
        lines_data = validated_data.pop("lines", None)
        new_status = validated_data.pop("status", None)
        user = validated_data.get("updated_by")

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if lines_data is not None:
            # Full replace: simplest correct behaviour for a nested collection.
            instance.lines.all().delete()
            self._create_lines(instance, lines_data, user)

        if new_status is not None:
            try:
                services.change_status(instance, new_status, user)
            except services.OrderRuleError as exc:
                raise serializers.ValidationError({"status": [str(exc)]}) from exc
        return instance
