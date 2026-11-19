"""Side by side (Wave 10b, ADR 0020): what was loaded from another ERP, and how its keys map to ours.

* ``ImportBatch`` - one file loaded: from which source system, which kind of object, how many rows
  were created, unchanged, updated or rejected, and the result of every row with its reason. The
  file's fingerprint (SHA-256 of company code, source, object and content) is unique: loading the
  same file again returns the first batch and touches nothing.
* ``ExternalReference`` - the other ERP's key for a record (customer ``C0001`` in Sage X3) and our
  number for it (business partner ``1000007``), with the fingerprint of the row last loaded. It
  makes every row idempotent on its own: the same row again is "unchanged", a changed partner or
  material is updated, and the same customer never becomes two business partners.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class ObjectType(models.TextChoices):
    """What a file holds. Load them in this order: later ones refer to earlier ones."""

    PARTNERS = "partners", "Business partners (customers, suppliers)"
    MATERIALS = "materials", "Materials"
    STOCK = "stock", "Stock on hand (a snapshot)"
    PURCHASE_ORDERS = "purchase_orders", "Open purchase orders"
    SALES_ORDERS = "sales_orders", "Open sales orders"


class ImportBatch(models.Model):
    """One file loaded from another ERP, with the result of each of its rows."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=30, help_text="The mapping used, e.g. generic or sage-x3.")
    object_type = models.CharField(max_length=20, choices=ObjectType.choices)
    file_name = models.CharField(max_length=200, blank=True, default="")
    fingerprint = models.CharField(max_length=64, unique=True)
    rows = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)
    results = models.JSONField(default=list, help_text="Per row: row, key, status, number, detail.")
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-imported_at", "-id"]
        verbose_name_plural = "import batches"

    def __str__(self):
        return f"{self.source} {self.object_type} ({self.rows} rows)"


class ExternalReference(models.Model):
    """The other ERP's key for one record, and our number for it."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=30)
    object_type = models.CharField(max_length=20, choices=ObjectType.choices)
    external_key = models.CharField(max_length=60)
    number = models.CharField(max_length=30, help_text="Our number: partner, material or document number.")
    row_fingerprint = models.CharField(max_length=64, help_text="SHA-256 of the row as last loaded.")
    first_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["source", "object_type", "external_key"]
        constraints = [models.UniqueConstraint(fields=["company_code", "source", "object_type", "external_key"],
                                               name="one_reference_per_external_key")]

    def __str__(self):
        return f"{self.source}:{self.object_type}:{self.external_key} -> {self.number}"
