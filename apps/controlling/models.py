"""Controlling (CO): who spent it and which part of the business earned it.

* ``ProfitCenter`` - a part of the business with its own result (a division, a branch).
* ``CostCenter`` - a place where costs arise (a department); belongs to one profit centre.
* ``PlantProfitCenter`` - the profit centre a plant's postings default to (stock, cost of
  sales, revenue).

Both are dimensions on the Universal Journal line (``finance.JournalLine``), so cost centre and
profit centre reports are sums over journal lines like every other report.
"""
from django.db import models

from apps.common.models import AuditedModel


class ProfitCenter(AuditedModel):
    """A part of the business whose profit is measured separately (SAP CO-PCA)."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, help_text="Inactive profit centres cannot be posted to.")

    class Meta:
        ordering = ["company_code__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "code"], name="unique_profit_center_per_company"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class CostCenter(AuditedModel):
    """A place where costs arise (a department or function), belonging to one profit centre."""

    class Category(models.TextChoices):
        MANAGEMENT = "MGMT", "Management and administration"
        SALES = "SALES", "Sales and distribution"
        PRODUCTION = "PROD", "Production and logistics"
        SERVICE = "SERV", "Service"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=5, choices=Category.choices, default=Category.MANAGEMENT)
    profit_center = models.ForeignKey(ProfitCenter, on_delete=models.PROTECT, related_name="cost_centers")
    manager = models.CharField(max_length=100, blank=True, help_text="Person responsible.")
    is_active = models.BooleanField(default=True, help_text="Inactive cost centres cannot be posted to.")

    class Meta:
        ordering = ["company_code__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "code"], name="unique_cost_center_per_company"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class PlantProfitCenter(AuditedModel):
    """Default profit centre for postings from a plant, used when a line names no other."""

    plant = models.OneToOneField("organization.Plant", on_delete=models.PROTECT, related_name="profit_center_link")
    profit_center = models.ForeignKey(ProfitCenter, on_delete=models.PROTECT, related_name="plants")

    class Meta:
        ordering = ["plant__code"]

    def __str__(self):
        return f"Plant {self.plant.code} -> {self.profit_center.code}"
