"""Master data shared by every module.

* Currency, ExchangeRate, UnitOfMeasure - reference data.
* BusinessPartner - one record per external party (S/4HANA style). The same
  partner can be a customer and/or a supplier, per company code, through role
  records that also name the reconciliation account their postings go to.
* Material - the material master: what the business buys, makes or sells.
"""
from datetime import date
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models, transaction

from apps.common.models import AuditedModel


class Currency(AuditedModel):
    """An ISO 4217 currency with its number of decimals."""

    code = models.CharField(max_length=3, unique=True, help_text="ISO 4217 code, e.g. ZAR.")
    name = models.CharField(max_length=100)
    decimal_places = models.PositiveSmallIntegerField(default=2)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "currencies"

    def __str__(self):
        return self.code


class ExchangeRateNotFound(LookupError):
    """No exchange rate is maintained for the currency pair on or before the date."""

    pass


class ExchangeRate(AuditedModel):
    """1 unit of ``from_currency`` = ``rate`` units of ``to_currency`` from ``valid_from`` onwards."""

    from_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="rates_from")
    to_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="rates_to")
    rate = models.DecimalField(max_digits=18, decimal_places=6, validators=[MinValueValidator(Decimal("0.000001"))])
    valid_from = models.DateField()

    class Meta:
        ordering = ["from_currency__code", "to_currency__code", "-valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["from_currency", "to_currency", "valid_from"], name="unique_exchange_rate_per_day"
            ),
            models.CheckConstraint(condition=models.Q(rate__gt=0), name="exchange_rate_positive"),
        ]

    def __str__(self):
        return f"1 {self.from_currency} = {self.rate} {self.to_currency} from {self.valid_from}"

    @classmethod
    def get_rate(cls, from_currency: Currency, to_currency: Currency, on: date) -> Decimal:
        """Latest rate valid on ``on``; uses the inverse rate if only that is maintained."""
        if from_currency.pk == to_currency.pk:
            return Decimal("1")
        direct = (
            cls.objects.filter(from_currency=from_currency, to_currency=to_currency, valid_from__lte=on)
            .order_by("-valid_from")
            .first()
        )
        if direct:
            return direct.rate
        inverse = (
            cls.objects.filter(from_currency=to_currency, to_currency=from_currency, valid_from__lte=on)
            .order_by("-valid_from")
            .first()
        )
        if inverse:
            return (Decimal("1") / inverse.rate).quantize(Decimal("0.000001"))
        raise ExchangeRateNotFound(f"No exchange rate {from_currency} -> {to_currency} valid on {on}.")


class UnitOfMeasure(AuditedModel):
    """A unit of measure such as EA, KG or HR."""

    code = models.CharField(max_length=3, unique=True, help_text="e.g. EA, KG, L, HR.")
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ["code"]
        verbose_name = "unit of measure"
        verbose_name_plural = "units of measure"

    def __str__(self):
        return self.code


class BusinessPartner(AuditedModel):
    """A person or organisation the company deals with (SAP BP). Customer and supplier are
    roles per company code, so one partner can be both.
    """

    class Category(models.TextChoices):
        ORGANIZATION = "ORGANIZATION", "Organisation"
        PERSON = "PERSON", "Person"

    NUMBER_BASE = 1_000_000

    number = models.CharField(max_length=10, unique=True, editable=False, blank=True)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.ORGANIZATION)
    name = models.CharField(max_length=200)
    vat_number = models.CharField(max_length=20, blank=True)
    registration_number = models.CharField(max_length=30, blank=True, help_text="Company registration or ID number.")
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    street = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=10, blank=True)
    country = models.CharField(max_length=2, default="ZA")
    is_blocked = models.BooleanField(default=False, help_text="Blocked partners cannot be used in new postings.")

    class Meta:
        ordering = ["number"]

    def save(self, *args, **kwargs):
        # Numbers are assigned by the system, like SAP's internal number range.
        """Give a new partner the next number from the partner range."""
        if self.number:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.number = str(self.NUMBER_BASE + self.pk)
            type(self).objects.filter(pk=self.pk).update(number=self.number)

    def has_customer_role(self, company_code) -> bool:
        """True if the partner is a customer of at least one company code."""
        return self.customer_roles.filter(company_code=company_code).exists()

    def has_supplier_role(self, company_code) -> bool:
        """True if the partner is a supplier of at least one company code."""
        return self.supplier_roles.filter(company_code=company_code).exists()

    def __str__(self):
        return f"{self.number} {self.name}"


class _PartnerRole(AuditedModel):
    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    reconciliation_account = models.ForeignKey(
        "finance.GLAccount", on_delete=models.PROTECT, related_name="+",
        help_text="G/L account that this partner's open items post to (receivables or payables).",
    )
    payment_terms_days = models.PositiveSmallIntegerField(default=30)

    class Meta:
        abstract = True


class CustomerRole(_PartnerRole):
    """A partner's customer data in one company code: reconciliation account, terms, credit limit."""

    business_partner = models.ForeignKey(BusinessPartner, on_delete=models.CASCADE, related_name="customer_roles")
    credit_limit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business_partner", "company_code"], name="unique_customer_role"),
        ]

    def __str__(self):
        return f"{self.business_partner} as customer in {self.company_code.code}"


class SupplierRole(_PartnerRole):
    """A partner's supplier data in one company code: reconciliation account and terms."""

    business_partner = models.ForeignKey(BusinessPartner, on_delete=models.CASCADE, related_name="supplier_roles")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business_partner", "company_code"], name="unique_supplier_role"),
        ]

    def __str__(self):
        return f"{self.business_partner} as supplier in {self.company_code.code}"


class Material(AuditedModel):
    """A product or service the company buys, makes, stocks or sells, with its type and base unit."""

    class MaterialType(models.TextChoices):
        TRADING_GOODS = "HAWA", "Trading goods"
        FINISHED = "FERT", "Finished product"
        RAW = "ROH", "Raw material"
        SERVICE = "DIEN", "Service"

    number = models.CharField(max_length=18, unique=True)
    description = models.CharField(max_length=200)
    material_type = models.CharField(max_length=4, choices=MaterialType.choices, default=MaterialType.TRADING_GOODS)
    base_unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="materials")
    material_group = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["number"]

    @property
    def is_stock_managed(self) -> bool:
        """Services are not kept in stock."""
        return self.material_type != self.MaterialType.SERVICE

    def __str__(self):
        return f"{self.number} {self.description}"


class MaterialPlant(AuditedModel):
    """The plant view of a material (SAP: plant data): the material is used at this plant.

    Created automatically on the first goods movement. Stock movements for one
    material in one plant are serialised by locking this row.
    """

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="plants")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="materials")

    class Meta:
        ordering = ["material__number", "plant__code"]
        constraints = [
            models.UniqueConstraint(fields=["material", "plant"], name="unique_material_per_plant"),
        ]

    def __str__(self):
        return f"{self.material.number} @ {self.plant.code}"
