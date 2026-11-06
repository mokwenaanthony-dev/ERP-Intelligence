from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.common.models import AuditedModel
from apps.inventory.models import Product


class Customer(AuditedModel):
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SalesOrder(AuditedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        SHIPPED = "SHIPPED", "Shipped"
        CANCELLED = "CANCELLED", "Cancelled"

    # Which status may follow which. SHIPPED and CANCELLED are final.
    ALLOWED_TRANSITIONS = {
        Status.PENDING: {Status.CONFIRMED, Status.CANCELLED},
        Status.CONFIRMED: {Status.SHIPPED, Status.CANCELLED},
        Status.SHIPPED: set(),
        Status.CANCELLED: set(),
    }

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    order_date = models.DateField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["-id"]

    @property
    def is_editable(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def total(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), start=Decimal("0.00"))

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.ALLOWED_TRANSITIONS[self.status]

    def __str__(self):
        return f"Order #{self.pk} - {self.customer.name}"


class SalesOrderLine(AuditedModel):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sales_lines")
    quantity = models.PositiveIntegerField()
    # Price at the moment of sale, so later catalogue price changes never
    # rewrite past orders.
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="sales_line_quantity_positive"),
            models.CheckConstraint(condition=models.Q(unit_price__gte=0), name="sales_line_price_non_negative"),
        ]

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price

    def save(self, *args, **kwargs):
        if self._state.adding and self.unit_price is None:
            self.unit_price = self.product.price
        super().save(*args, **kwargs)

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError("Quantity must be greater than zero.")

    def __str__(self):
        return f"{self.quantity} x {self.product.name} @ {self.unit_price}"
