"""Sales and distribution: order-to-cash.

    Sales order -> Delivery (goods issue 601) -> Invoice (receivable + revenue + VAT) -> Payment (finance)

* ``SalesPrice`` - price condition per material and sales organisation, valid from a date.
* ``RevenueAccountSetting`` - which revenue account and default VAT code a material type uses.
* ``SalesOrder`` / ``SalesOrderItem`` - what the customer ordered. Delivered and invoiced
  quantities are derived from the deliveries and invoices, never stored.
* ``Delivery`` - goods leaving the plant; posts a 601 goods issue (cost of sales).
* ``Invoice`` - billing document for one delivery; posts the receivable.

Every document is created through ``apps.sales.services``.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum

from apps.common.models import AuditedModel


class SalesPrice(AuditedModel):
    """Net selling price (excluding VAT) per base unit, in the company currency."""

    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="sales_prices")
    sales_organization = models.ForeignKey(
        "organization.SalesOrganization", on_delete=models.PROTECT, related_name="prices"
    )
    price = models.DecimalField(max_digits=18, decimal_places=2)
    valid_from = models.DateField()

    class Meta:
        ordering = ["material__number", "sales_organization__code", "-valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["material", "sales_organization", "valid_from"], name="unique_sales_price_per_day"
            ),
            models.CheckConstraint(condition=Q(price__gte=0), name="sales_price_non_negative"),
        ]

    def __str__(self):
        return f"{self.material.number} @ {self.sales_organization.code}: {self.price} from {self.valid_from}"


class RevenueAccountSetting(AuditedModel):
    """Revenue account and default output VAT code per chart and (optionally) material type."""

    chart_of_accounts = models.ForeignKey(
        "finance.ChartOfAccounts", on_delete=models.PROTECT, related_name="revenue_accounts"
    )
    material_type = models.CharField(max_length=4, blank=True, help_text="Blank = all material types.")
    revenue_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")
    default_tax_code = models.ForeignKey("finance.TaxCode", on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["chart_of_accounts__code", "material_type"]
        constraints = [
            models.UniqueConstraint(fields=["chart_of_accounts", "material_type"], name="unique_revenue_setting"),
        ]

    def __str__(self):
        return f"{self.chart_of_accounts.code} {self.material_type or 'all'} -> {self.revenue_account.number}"


class SalesOrder(AuditedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"  # being entered; items can change
        CONFIRMED = "CONFIRMED", "Confirmed"  # released for delivery
        COMPLETED = "COMPLETED", "Completed"  # fully delivered and invoiced
        CANCELLED = "CANCELLED", "Cancelled"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    sales_organization = models.ForeignKey(
        "organization.SalesOrganization", on_delete=models.PROTECT, related_name="orders"
    )
    order_number = models.CharField(max_length=12)
    customer = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="sales_orders")
    order_date = models.DateField()
    requested_delivery_date = models.DateField(null=True, blank=True)
    customer_reference = models.CharField(max_length=35, blank=True, help_text="The customer's own order number.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    class Meta:
        ordering = ["-order_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "order_number"], name="unique_sales_order_number"),
        ]

    @property
    def net_total(self) -> Decimal:
        return sum((item.net_amount for item in self.items.all()), Decimal("0.00"))

    def __str__(self):
        return f"{self.order_number} {self.customer.name}"


class SalesOrderItem(AuditedModel):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="items")
    item_number = models.PositiveSmallIntegerField(help_text="10, 20, 30 ...")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit = models.ForeignKey("masterdata.UnitOfMeasure", on_delete=models.PROTECT, related_name="+")
    net_price = models.DecimalField(max_digits=18, decimal_places=2, help_text="Per base unit, excluding VAT.")
    price_overridden = models.BooleanField(default=False, help_text="True if the price was entered manually.")
    tax_code = models.ForeignKey("finance.TaxCode", on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["order_id", "item_number"]
        constraints = [
            models.UniqueConstraint(fields=["order", "item_number"], name="unique_item_per_sales_order"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="sales_item_quantity_positive"),
            models.CheckConstraint(condition=Q(net_price__gte=0), name="sales_item_price_non_negative"),
        ]

    @property
    def net_amount(self) -> Decimal:
        return (self.quantity * self.net_price).quantize(Decimal("0.01"))

    @property
    def delivered_quantity(self) -> Decimal:
        total = self.delivery_items.filter(delivery__status=Delivery.Status.POSTED).aggregate(q=Sum("quantity"))["q"]
        return Decimal(total or 0).quantize(Decimal("0.001"))

    @property
    def invoiced_quantity(self) -> Decimal:
        total = self.invoice_items.filter(invoice__status=Invoice.Status.POSTED).aggregate(q=Sum("quantity"))["q"]
        return Decimal(total or 0).quantize(Decimal("0.001"))

    @property
    def open_quantity(self) -> Decimal:
        return self.quantity - self.delivered_quantity

    def __str__(self):
        return f"{self.order.order_number}/{self.item_number} {self.material.number} x {self.quantity}"


class Delivery(AuditedModel):
    class Status(models.TextChoices):
        POSTED = "POSTED", "Goods issue posted"
        REVERSED = "REVERSED", "Reversed"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    delivery_number = models.CharField(max_length=12)
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="deliveries")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    delivery_date = models.DateField()
    material_document = models.OneToOneField(
        "inventory.MaterialDocument", on_delete=models.PROTECT, related_name="delivery",
        help_text="The 601 goods issue that took the stock out.",
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.POSTED)

    class Meta:
        ordering = ["-delivery_date", "-id"]
        verbose_name_plural = "deliveries"
        constraints = [
            models.UniqueConstraint(fields=["company_code", "delivery_number"], name="unique_delivery_number"),
        ]

    def __str__(self):
        return f"{self.delivery_number} for {self.sales_order.order_number}"


class DeliveryItem(models.Model):
    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey(SalesOrderItem, on_delete=models.PROTECT, related_name="delivery_items")
    storage_location = models.ForeignKey("organization.StorageLocation", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        ordering = ["delivery_id", "order_item__item_number"]

    def __str__(self):
        return f"{self.delivery.delivery_number}: {self.order_item.material.number} x {self.quantity}"


class Invoice(AuditedModel):
    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    invoice_number = models.CharField(max_length=12)
    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="invoices")
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="invoices")
    invoice_date = models.DateField()
    due_date = models.DateField()
    net_amount = models.DecimalField(max_digits=18, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=18, decimal_places=2)
    journal_entry = models.OneToOneField(
        "finance.JournalEntry", on_delete=models.PROTECT, related_name="sales_invoice"
    )
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.POSTED)

    class Meta:
        ordering = ["-invoice_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "invoice_number"], name="unique_invoice_number"),
        ]

    def __str__(self):
        return f"{self.invoice_number} {self.customer.name} {self.gross_amount}"


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey(SalesOrderItem, on_delete=models.PROTECT, related_name="invoice_items")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    net_price = models.DecimalField(max_digits=18, decimal_places=2)
    net_amount = models.DecimalField(max_digits=18, decimal_places=2)
    tax_code = models.ForeignKey("finance.TaxCode", on_delete=models.PROTECT, related_name="+")
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        ordering = ["invoice_id", "order_item__item_number"]

    def __str__(self):
        return f"{self.invoice.invoice_number}: {self.order_item.material.number} x {self.quantity}"
