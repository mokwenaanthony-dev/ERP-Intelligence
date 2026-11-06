from django.db import models

from apps.common.models import AuditedModel


class Product(AuditedModel):
    """Catalogue item.

    ``stock`` is a single on-hand quantity for now. Wave 2 replaces it with a
    stock-movement ledger (plants, storage locations, movement types).
    """

    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, unique=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.IntegerField(default=0)

    class Meta:
        ordering = ["sku"]
        constraints = [
            models.CheckConstraint(condition=models.Q(stock__gte=0), name="product_stock_non_negative"),
            models.CheckConstraint(condition=models.Q(price__gte=0), name="product_price_non_negative"),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"
