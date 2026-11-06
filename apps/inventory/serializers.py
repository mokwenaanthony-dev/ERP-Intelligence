from rest_framework import serializers

from .models import Product


class ProductSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    stock = serializers.IntegerField(min_value=0, required=False)

    class Meta:
        model = Product
        fields = ["id", "name", "sku", "price", "stock", "created_at", "updated_at", "created_by", "updated_by"]
        read_only_fields = ["id", "created_at", "updated_at", "created_by", "updated_by"]
