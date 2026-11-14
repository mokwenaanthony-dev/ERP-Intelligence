"""Supplier relationship management (Wave 7b): finding, buying from and judging suppliers.

    Supplier application -> (approve) -> Business Partner with a supplier role
    Request for quotation -> invited suppliers quote -> comparison -> (award) -> Purchase order
    Contract (agreed price, target quantity) -> (release) -> Purchase order, up to the target
    Supplier evaluation: on time, in full, at the agreed price, quality -> score and grade A-D

* ``SupplierApplication`` - a company asking to become a supplier. Due diligence (registration
  number, VAT number, B-BBEE level) is checked before approval. Its contact person's details are
  **personal data**: never logged, never in the graph (ADR 0013).
* ``RequestForQuotation`` / ``RFQItem`` - what we want to buy, sent to several suppliers.
* ``SupplierQuote`` / ``QuoteItem`` - one invited supplier's prices and delivery time.
* ``Contract`` / ``ContractItem`` - an outline agreement: a price per material for a period, up to a
  target quantity. ``ContractRelease`` links each purchase order released against it.
* ``SupplierEvaluation`` - a supplier's measured performance in a period, with a grade.

Documents are numbered continuously per company code: OB (onboarding), RQ (RFQ), CT (contract).
Every change of status goes through ``apps.srm.services``.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q

from apps.common.models import AuditedModel


class SupplierApplication(AuditedModel):
    """A company that wants to supply us. Approving it creates the supplier."""

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"  # now a supplier
        REJECTED = "REJECTED", "Rejected"  # the reason is kept

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    application_number = models.CharField(max_length=12)
    company_name = models.CharField(max_length=200)
    registration_number = models.CharField(max_length=30, blank=True, help_text="CIPC company registration number.")
    vat_number = models.CharField(max_length=20, blank=True)
    bbbee_level = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(8)],
        help_text="B-BBEE contributor level 1 (best) to 8; empty = non-compliant or unknown.")
    supplies = models.CharField(max_length=200, blank=True, help_text="What the company offers, in a few words.")
    # Personal data (POPIA): seen by purchasing only, never logged or projected into the graph.
    contact_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.SUBMITTED)
    rejection_reason = models.CharField(max_length=200, blank=True)
    business_partner = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, null=True,
                                         blank=True, related_name="+", help_text="Set when approved.")

    class Meta:
        ordering = ["company_code__code", "-application_number"]
        constraints = [models.UniqueConstraint(fields=["company_code", "application_number"],
                                               name="unique_application_number")]

    def __str__(self):
        return f"{self.company_code.code}/{self.application_number}"


class RequestForQuotation(AuditedModel):
    """What we want to buy, sent to several suppliers for their prices until a deadline."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"  # suppliers may quote until the deadline
        AWARDED = "AWARDED", "Awarded"  # the purchase order exists
        CANCELLED = "CANCELLED", "Cancelled"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    purchasing_organization = models.ForeignKey("organization.PurchasingOrganization", on_delete=models.PROTECT,
                                                related_name="+")
    rfq_number = models.CharField(max_length=12)
    title = models.CharField(max_length=200)
    deadline = models.DateField(help_text="Last day suppliers may quote.")
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.DRAFT)
    purchase_order = models.OneToOneField("purchasing.PurchaseOrder", on_delete=models.PROTECT, null=True,
                                          blank=True, related_name="rfq")

    class Meta:
        ordering = ["company_code__code", "-rfq_number"]
        verbose_name = "request for quotation"
        verbose_name_plural = "requests for quotation"
        constraints = [models.UniqueConstraint(fields=["company_code", "rfq_number"], name="unique_rfq_number")]

    def __str__(self):
        return f"{self.company_code.code}/{self.rfq_number} {self.title}"


class RFQItem(models.Model):
    """One material asked for."""

    rfq = models.ForeignKey(RequestForQuotation, on_delete=models.CASCADE, related_name="items")
    item_number = models.PositiveIntegerField()
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        ordering = ["rfq", "item_number"]
        constraints = [
            models.UniqueConstraint(fields=["rfq", "item_number"], name="unique_rfq_item"),
            models.UniqueConstraint(fields=["rfq", "material"], name="unique_rfq_material"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="rfq_quantity_positive"),
        ]


class SupplierQuote(AuditedModel):
    """One invited supplier's answer to a request for quotation."""

    class Status(models.TextChoices):
        INVITED = "INVITED", "Invited"  # no prices yet
        SUBMITTED = "SUBMITTED", "Submitted"
        AWARDED = "AWARDED", "Awarded"
        LOST = "LOST", "Lost"

    rfq = models.ForeignKey(RequestForQuotation, on_delete=models.CASCADE, related_name="quotes")
    supplier = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="rfq_quotes")
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.INVITED)
    delivery_days = models.PositiveIntegerField(null=True, blank=True, help_text="Days from order to delivery.")
    submitted_on = models.DateField(null=True, blank=True)
    supplier_reference = models.CharField(max_length=35, blank=True, help_text="The supplier's quotation number.")

    class Meta:
        ordering = ["rfq", "supplier__number"]
        constraints = [models.UniqueConstraint(fields=["rfq", "supplier"], name="one_quote_per_supplier")]

    def __str__(self):
        return f"{self.rfq.rfq_number} / {self.supplier.number}"


class QuoteItem(models.Model):
    """A supplier's price for one item of the request."""

    quote = models.ForeignKey(SupplierQuote, on_delete=models.CASCADE, related_name="items")
    rfq_item = models.ForeignKey(RFQItem, on_delete=models.CASCADE, related_name="+")
    net_price = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        ordering = ["quote", "rfq_item__item_number"]
        constraints = [
            models.UniqueConstraint(fields=["quote", "rfq_item"], name="one_price_per_item"),
            models.CheckConstraint(condition=Q(net_price__gte=0), name="quote_price_non_negative"),
        ]


class Contract(AuditedModel):
    """An outline agreement with a supplier: agreed prices for a period, up to a target quantity per
    material. Purchase orders are released against it."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Cancelled"

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    purchasing_organization = models.ForeignKey("organization.PurchasingOrganization", on_delete=models.PROTECT,
                                                related_name="+")
    contract_number = models.CharField(max_length=12)
    supplier = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="contracts")
    valid_from = models.DateField()
    valid_to = models.DateField()
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["company_code__code", "-contract_number"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "contract_number"], name="unique_contract_number"),
            models.CheckConstraint(condition=Q(valid_to__gte=F("valid_from")), name="contract_ends_after_start"),
        ]

    def __str__(self):
        return f"{self.company_code.code}/{self.contract_number}"


class ContractItem(models.Model):
    """A material on a contract: its agreed price and how much may be bought at it."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="items")
    item_number = models.PositiveIntegerField()
    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    net_price = models.DecimalField(max_digits=18, decimal_places=2)
    target_quantity = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        ordering = ["contract", "item_number"]
        constraints = [
            models.UniqueConstraint(fields=["contract", "material"], name="unique_contract_material"),
            models.CheckConstraint(condition=Q(net_price__gte=0), name="contract_price_non_negative"),
            models.CheckConstraint(condition=Q(target_quantity__gt=0), name="contract_target_positive"),
        ]


class ContractRelease(models.Model):
    """A purchase order released against a contract (its quantities count towards the targets)."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="releases")
    purchase_order = models.OneToOneField("purchasing.PurchaseOrder", on_delete=models.PROTECT,
                                          related_name="contract_release")

    class Meta:
        ordering = ["contract", "purchase_order"]


class SupplierEvaluation(models.Model):
    """A supplier's performance in a period: measured from receipts and invoices, plus a quality score.

    Evaluating the same supplier and period again replaces the scores (one row per period).
    """

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    supplier = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="evaluations")
    period_start = models.DateField()
    period_end = models.DateField()
    # Percentages 0-100, None when there was nothing to measure in the period.
    on_time_rate = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True,
                                       help_text="Goods receipts on or before the order's delivery date.")
    in_full_rate = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True,
                                       help_text="Quantity received against quantity ordered, per order item.")
    price_rate = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True,
                                     help_text="Invoice items at exactly the order price.")
    quality_score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="1 (poor) to 5 (excellent), from the users of the goods.")
    overall_score = models.DecimalField(max_digits=5, decimal_places=1)
    grade = models.CharField(max_length=1)
    evaluated_on = models.DateField()
    evaluated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")

    class Meta:
        ordering = ["supplier__number", "-period_end"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "supplier", "period_start", "period_end"],
                                    name="one_evaluation_per_period"),
            models.CheckConstraint(condition=Q(period_end__gte=F("period_start")), name="evaluation_period_valid"),
        ]

    def __str__(self):
        return f"{self.supplier.number} {self.period_start}..{self.period_end}: {self.grade}"
