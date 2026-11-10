"""Inventory management.

Wave 2a adds a stock-movement ledger modelled on SAP inventory management:

* ``MovementType`` - what a movement does (receipt, issue, transfer, count difference).
* ``MaterialDocument`` / ``MaterialDocumentItem`` - the immutable record of every
  movement. Items carry *signed* quantities and values, so stock on hand and
  stock value are plain sums over the items: there are no stored totals.
* ``InventoryAccountSetting`` - account determination: which G/L accounts a
  movement posts to. Every value-changing movement posts a journal entry.

Materials come from ``masterdata.Material``; the Wave 0 ``Product`` catalogue was retired in Wave 2b.
"""
from django.db import models

from apps.common.models import AuditedModel
from apps.finance.models import ImmutableModel


class MovementType(models.TextChoices):
    """SAP movement type codes."""

    GOODS_RECEIPT = "101", "Goods receipt"
    INITIAL_ENTRY = "561", "Initial entry of stock"
    GOODS_ISSUE_DELIVERY = "601", "Goods issue for delivery"
    SCRAPPING = "551", "Scrapping"
    INVENTORY_GAIN = "701", "Physical inventory gain"
    INVENTORY_LOSS = "702", "Physical inventory loss"
    TRANSFER = "311", "Transfer between storage locations"
    GOODS_ISSUE_ORDER = "261", "Goods issue to production order"


RECEIPTS = {MovementType.GOODS_RECEIPT, MovementType.INITIAL_ENTRY, MovementType.INVENTORY_GAIN}
ISSUES = {MovementType.GOODS_ISSUE_DELIVERY, MovementType.SCRAPPING, MovementType.INVENTORY_LOSS,
          MovementType.GOODS_ISSUE_ORDER}
PRICED_RECEIPTS = {MovementType.GOODS_RECEIPT, MovementType.INITIAL_ENTRY}  # value comes from the given price


class AccountKey(models.TextChoices):
    """Purpose of an account in inventory postings (SAP transaction keys in brackets)."""

    INVENTORY = "INVENTORY", "Inventory (BSX)"
    GR_IR = "GR_IR", "Goods received / invoice received clearing (WRX)"
    INITIAL_ENTRY = "INITIAL_ENTRY", "Initial stock entry offset"
    COST_OF_SALES = "COST_OF_SALES", "Cost of goods sold (GBB-VAX)"
    SCRAP = "SCRAP", "Scrapping expense (GBB-VNG)"
    DIFFERENCES = "DIFFERENCES", "Physical inventory differences (GBB-INV)"
    # Values kept within the 15-character column.
    CONSUMPTION = "CONSUMPTION", "Consumption for production orders (GBB-VBR)"
    FACTORY_OUTPUT = "FACTORY_OUTPUT", "Factory output credited to production orders"
    PROD_VARIANCE = "PROD_VARIANCE", "Production order variances (settlement)"


# Which account each value-changing movement uses opposite the inventory account.
OFFSET_ACCOUNT = {
    MovementType.GOODS_RECEIPT: AccountKey.GR_IR,
    MovementType.INITIAL_ENTRY: AccountKey.INITIAL_ENTRY,
    MovementType.GOODS_ISSUE_DELIVERY: AccountKey.COST_OF_SALES,
    MovementType.SCRAPPING: AccountKey.SCRAP,
    MovementType.INVENTORY_GAIN: AccountKey.DIFFERENCES,
    MovementType.INVENTORY_LOSS: AccountKey.DIFFERENCES,
    MovementType.GOODS_ISSUE_ORDER: AccountKey.CONSUMPTION,
}
# A 101 that receives a product from a production order (instead of from a purchase order) is
# offset against the order, not against GR/IR.
ORDER_RECEIPT_OFFSET = AccountKey.FACTORY_OUTPUT


class InventoryAccountSetting(AuditedModel):
    """Account determination per chart of accounts, account key and (optionally) material type.

    A setting with a blank material type is the default for all material types.
    """

    chart_of_accounts = models.ForeignKey(
        "finance.ChartOfAccounts", on_delete=models.PROTECT, related_name="inventory_accounts"
    )
    account_key = models.CharField(max_length=15, choices=AccountKey.choices)
    material_type = models.CharField(max_length=4, blank=True, help_text="Blank = all material types.")
    gl_account = models.ForeignKey("finance.GLAccount", on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["chart_of_accounts__code", "account_key", "material_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["chart_of_accounts", "account_key", "material_type"], name="unique_inventory_account_setting"
            ),
        ]

    def __str__(self):
        scope = self.material_type or "all"
        return f"{self.chart_of_accounts.code} {self.account_key} ({scope}) -> {self.gl_account.number}"


class MaterialDocument(ImmutableModel, AuditedModel):
    """Header of a goods movement. Created only through ``apps.inventory.services``."""

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    company_code = models.ForeignKey(
        "organization.CompanyCode", on_delete=models.PROTECT, related_name="material_documents"
    )
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="material_documents")
    fiscal_year = models.PositiveSmallIntegerField()
    document_number = models.CharField(max_length=12)
    movement_type = models.CharField(max_length=3, choices=MovementType.choices)
    posting_date = models.DateField()
    document_date = models.DateField()
    reference = models.CharField(max_length=30, blank=True)
    order_number = models.CharField(max_length=20, blank=True, default="", db_index=True,
                                    help_text="Production order issued to (261) or received from (101).")
    header_text = models.CharField(max_length=100, blank=True)
    business_partner = models.ForeignKey(
        "masterdata.BusinessPartner", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Supplier for receipts, customer for deliveries.",
    )
    journal_entry = models.OneToOneField(
        "finance.JournalEntry", on_delete=models.PROTECT, null=True, blank=True, related_name="material_document",
        help_text="The accounting document. Empty for movements that do not change value (transfers).",
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.POSTED)
    reversal_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversed_by"
    )

    class Meta:
        ordering = ["-posting_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "fiscal_year", "document_number"], name="unique_material_document_number"
            ),
        ]

    def __str__(self):
        return f"{self.company_code.code}/{self.fiscal_year}/{self.document_number} ({self.movement_type})"


class MaterialDocumentItem(ImmutableModel):
    """One stock effect: signed quantity in a storage location, signed value in company currency.

    A transfer produces two items (minus at the source, plus at the destination, value zero).
    """

    document = models.ForeignKey(MaterialDocument, on_delete=models.PROTECT, related_name="items")
    line_number = models.PositiveSmallIntegerField()
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="stock_movements")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    storage_location = models.ForeignKey(
        "organization.StorageLocation", on_delete=models.PROTECT, related_name="stock_movements"
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=3, help_text="Signed, in the base unit.")
    unit = models.ForeignKey("masterdata.UnitOfMeasure", on_delete=models.PROTECT, related_name="+")
    amount_local = models.DecimalField(max_digits=18, decimal_places=2, help_text="Signed value change.")
    text = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_id", "line_number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "line_number"], name="unique_item_number_per_document"),
        ]
        indexes = [
            models.Index(fields=["material", "plant"], name="mdi_material_plant_idx"),
            models.Index(fields=["material", "storage_location"], name="mdi_material_sloc_idx"),
        ]

    def __str__(self):
        return f"{self.document} item {self.line_number}: {self.material.number} {self.quantity}"
