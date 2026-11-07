"""Materials management: procure-to-pay.

    Purchase order -> Goods receipt (101: Dr inventory / Cr GR/IR) -> Supplier invoice
    (three-way match: Dr GR/IR + price difference + input VAT / Cr payables) -> Payment (finance)

* ``PurchasingAccountSetting`` - per chart of accounts: default input VAT code, the price
  difference account and the invoice price tolerance (SAP: account key PRD, tolerance keys).
* ``PurchaseOrder`` / ``PurchaseOrderItem`` - what was ordered from the supplier and at what
  price. Received and invoiced quantities are derived from the documents, never stored.
* ``GoodsReceipt`` - goods arriving against the order; posts a 101 receipt at the order price.
* ``SupplierInvoice`` - the supplier's bill, matched against order and receipts; posts the payable.

Every document is created through ``apps.purchasing.services``.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum

from apps.common.models import AuditedModel

QTY = Decimal("0.001")
CENT = Decimal("0.01")


class PurchasingAccountSetting(AuditedModel):
    """Per chart of accounts: the default input VAT code and the price-difference account."""

    chart_of_accounts = models.OneToOneField(
        "finance.ChartOfAccounts", on_delete=models.PROTECT, related_name="purchasing_setting"
    )
    default_tax_code = models.ForeignKey("finance.TaxCode", on_delete=models.PROTECT, related_name="+")
    price_difference_account = models.ForeignKey(
        "finance.GLAccount", on_delete=models.PROTECT, related_name="+",
        help_text="Takes the difference between the invoice price and the order price.",
    )
    price_tolerance_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("5.00"),
        help_text="Invoices whose unit price differs from the order price by more than this are rejected.",
    )

    class Meta:
        ordering = ["chart_of_accounts__code"]
        constraints = [
            models.CheckConstraint(condition=Q(price_tolerance_percent__gte=0), name="price_tolerance_non_negative"),
        ]

    def __str__(self):
        return f"{self.chart_of_accounts.code}: PRD {self.price_difference_account.number}, " \
               f"tolerance {self.price_tolerance_percent}%"


class PurchaseOrder(AuditedModel):
    """An order to a supplier. Received and invoiced quantities are derived from its documents."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"  # being entered; items can change
        RELEASED = "RELEASED", "Released"  # sent to the supplier; goods can be received
        COMPLETED = "COMPLETED", "Completed"  # fully received and invoiced
        CANCELLED = "CANCELLED", "Cancelled"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    purchasing_organization = models.ForeignKey(
        "organization.PurchasingOrganization", on_delete=models.PROTECT, related_name="orders"
    )
    order_number = models.CharField(max_length=12)
    supplier = models.ForeignKey(
        "masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="purchase_orders"
    )
    order_date = models.DateField()
    delivery_date = models.DateField(null=True, blank=True, help_text="When the goods are expected.")
    supplier_reference = models.CharField(max_length=35, blank=True, help_text="The supplier's quotation number.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    class Meta:
        ordering = ["-order_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "order_number"], name="unique_purchase_order_number"),
        ]

    @property
    def net_total(self) -> Decimal:
        """Sum of the items' net amounts."""
        return sum((item.net_amount for item in self.items.all()), Decimal("0.00"))

    def __str__(self):
        return f"{self.order_number} {self.supplier.name}"


class PurchaseOrderItem(AuditedModel):
    """One material on a purchase order, with quantity, price, plant and storage location."""

    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item_number = models.PositiveSmallIntegerField(help_text="10, 20, 30 ...")
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    storage_location = models.ForeignKey(
        "organization.StorageLocation", on_delete=models.PROTECT, related_name="+",
        help_text="Where the goods are received by default.",
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit = models.ForeignKey("masterdata.UnitOfMeasure", on_delete=models.PROTECT, related_name="+")
    net_price = models.DecimalField(max_digits=18, decimal_places=2, help_text="Per base unit, excluding VAT.")
    tax_code = models.ForeignKey("finance.TaxCode", on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["order_id", "item_number"]
        constraints = [
            models.UniqueConstraint(fields=["order", "item_number"], name="unique_item_per_purchase_order"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="purchase_item_quantity_positive"),
            models.CheckConstraint(condition=Q(net_price__gte=0), name="purchase_item_price_non_negative"),
        ]

    @property
    def net_amount(self) -> Decimal:
        """Quantity times net price, rounded to cents."""
        return (self.quantity * self.net_price).quantize(CENT)

    def _posted_receipts(self):
        return self.receipt_items.filter(receipt__status=GoodsReceipt.Status.POSTED)

    def _posted_invoices(self):
        return self.invoice_items.filter(invoice__status=SupplierInvoice.Status.POSTED)

    @property
    def received_quantity(self) -> Decimal:
        """Quantity received on posted goods receipts (reversed receipts excluded)."""
        return Decimal(self._posted_receipts().aggregate(q=Sum("quantity"))["q"] or 0).quantize(QTY)

    @property
    def received_value(self) -> Decimal:
        """Value posted to GR/IR by the posted goods receipts."""
        return Decimal(self._posted_receipts().aggregate(v=Sum("value"))["v"] or 0).quantize(CENT)

    @property
    def invoiced_quantity(self) -> Decimal:
        """Quantity on posted supplier invoices (cancelled invoices excluded)."""
        return Decimal(self._posted_invoices().aggregate(q=Sum("quantity"))["q"] or 0).quantize(QTY)

    @property
    def invoiced_value(self) -> Decimal:
        """GR/IR cleared by invoices (at the order price)."""
        return Decimal(self._posted_invoices().aggregate(v=Sum("gr_ir_amount"))["v"] or 0).quantize(CENT)

    @property
    def open_quantity(self) -> Decimal:
        """Still to be received."""
        return self.quantity - self.received_quantity

    @property
    def to_invoice_quantity(self) -> Decimal:
        """Received but not yet invoiced."""
        return self.received_quantity - self.invoiced_quantity

    def __str__(self):
        return f"{self.order.order_number}/{self.item_number} {self.material.number} x {self.quantity}"


class GoodsReceipt(AuditedModel):
    """Receipt of ordered goods (movement 101). Posts stock and GR/IR; reversed, not edited."""

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    receipt_number = models.CharField(max_length=12)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    receipt_date = models.DateField()
    delivery_note = models.CharField(max_length=35, blank=True, help_text="The supplier's delivery note number.")
    material_document = models.OneToOneField(
        "inventory.MaterialDocument", on_delete=models.PROTECT, related_name="goods_receipt",
        help_text="The 101 movement that put the stock in.",
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.POSTED)

    class Meta:
        ordering = ["-receipt_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "receipt_number"], name="unique_goods_receipt_number"),
        ]

    def __str__(self):
        return f"{self.receipt_number} for {self.purchase_order.order_number}"


class GoodsReceiptItem(models.Model):
    """One received purchase order item."""

    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey(PurchaseOrderItem, on_delete=models.PROTECT, related_name="receipt_items")
    storage_location = models.ForeignKey("organization.StorageLocation", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    value = models.DecimalField(max_digits=18, decimal_places=2, help_text="Posted to inventory and GR/IR.")

    class Meta:
        ordering = ["receipt_id", "order_item__item_number"]

    def __str__(self):
        return f"{self.receipt.receipt_number}: {self.order_item.material.number} x {self.quantity}"


class SupplierInvoice(AuditedModel):
    """A supplier's invoice matched to a purchase order and its receipts (three-way match)."""

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    invoice_number = models.CharField(max_length=12, help_text="Our number for the invoice.")
    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    supplier = models.ForeignKey(
        "masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="supplier_invoices"
    )
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="invoices")
    supplier_reference = models.CharField(max_length=35, help_text="The supplier's own invoice number.")
    invoice_date = models.DateField()
    due_date = models.DateField()
    net_amount = models.DecimalField(max_digits=18, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=18, decimal_places=2)
    journal_entry = models.OneToOneField(
        "finance.JournalEntry", on_delete=models.PROTECT, related_name="supplier_invoice"
    )
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.POSTED)

    class Meta:
        ordering = ["-invoice_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "invoice_number"], name="unique_supplier_invoice_number"),
        ]

    def __str__(self):
        return f"{self.invoice_number} ({self.supplier_reference}) {self.supplier.name} {self.gross_amount}"


class SupplierInvoiceItem(models.Model):
    """One invoiced purchase order item."""

    invoice = models.ForeignKey(SupplierInvoice, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey(PurchaseOrderItem, on_delete=models.PROTECT, related_name="invoice_items")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    net_price = models.DecimalField(max_digits=18, decimal_places=2, help_text="Unit price on the invoice.")
    net_amount = models.DecimalField(max_digits=18, decimal_places=2)
    gr_ir_amount = models.DecimalField(max_digits=18, decimal_places=2, help_text="GR/IR cleared at order price.")
    price_difference = models.DecimalField(max_digits=18, decimal_places=2)
    tax_code = models.ForeignKey("finance.TaxCode", on_delete=models.PROTECT, related_name="+")
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        ordering = ["invoice_id", "order_item__item_number"]

    def __str__(self):
        return f"{self.invoice.invoice_number}: {self.order_item.material.number} x {self.quantity}"
