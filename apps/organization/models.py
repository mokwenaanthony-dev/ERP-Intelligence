"""Enterprise structure (SAP: "organisational units").

* CompanyCode - a legal entity that keeps its own books: currency, chart of
  accounts and fiscal year. Every journal entry belongs to exactly one.
* Plant - a site where goods are made, stored or sold from.
* StorageLocation - a place inside a plant where stock is kept.
* SalesOrganization / PurchasingOrganization - the units that sell and buy on
  behalf of a company code.
"""
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from apps.common.models import AuditedModel

org_code = RegexValidator(r"^[A-Z0-9]{1,4}$", "Use 1-4 upper-case letters or digits.")


class CompanyCode(AuditedModel):
    code = models.CharField(max_length=4, unique=True, validators=[org_code])
    name = models.CharField(max_length=200)
    country = models.CharField(max_length=2, default="ZA", help_text="ISO 3166 country code.")
    currency = models.ForeignKey(
        "masterdata.Currency", on_delete=models.PROTECT, related_name="company_codes",
        help_text="Local (company) currency that the books are kept in.",
    )
    chart_of_accounts = models.ForeignKey(
        "finance.ChartOfAccounts", on_delete=models.PROTECT, related_name="company_codes"
    )
    vat_registration_number = models.CharField(max_length=20, blank=True)
    fiscal_year_start_month = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        help_text="Month the fiscal year starts in (3 = March, common in South Africa).",
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class Plant(AuditedModel):
    code = models.CharField(max_length=4, unique=True, validators=[org_code])
    name = models.CharField(max_length=200)
    company_code = models.ForeignKey(CompanyCode, on_delete=models.PROTECT, related_name="plants")
    address = models.TextField(blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class StorageLocation(AuditedModel):
    plant = models.ForeignKey(Plant, on_delete=models.PROTECT, related_name="storage_locations")
    code = models.CharField(max_length=4, validators=[org_code])
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["plant__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["plant", "code"], name="unique_storage_location_per_plant"),
        ]

    def __str__(self):
        return f"{self.plant.code}/{self.code} {self.name}"


class SalesOrganization(AuditedModel):
    code = models.CharField(max_length=4, unique=True, validators=[org_code])
    name = models.CharField(max_length=200)
    company_code = models.ForeignKey(CompanyCode, on_delete=models.PROTECT, related_name="sales_organizations")

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class PurchasingOrganization(AuditedModel):
    code = models.CharField(max_length=4, unique=True, validators=[org_code])
    name = models.CharField(max_length=200)
    company_code = models.ForeignKey(
        CompanyCode, on_delete=models.PROTECT, related_name="purchasing_organizations"
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"
