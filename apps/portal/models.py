"""The partner network (Wave 10a, ADR 0019): who outside the company may log in, and what they told us.

* ``PartnerAccess`` - links one portal user to one business partner in one company code. Every
  portal query is filtered through it, so a supplier sees its own purchase orders and nothing else.
* ``OrderConfirmation`` - a supplier's confirmation of a purchase order line: the quantity and the
  date it will deliver. Kept as a history (each changed confirmation is a new row; the latest
  counts); the same confirmation again adds nothing. MRP plans the receipt on the confirmed date and
  the control tower flags a date later than agreed.
* ``DeliveryNotice`` - an advance shipping notice: the supplier says what is on the way and when it
  arrives. Identified by the supplier's own reference: sending the same notice again returns the
  first; a different notice under the same reference is refused.
* ``ForecastShare`` - purchasing's agreement to share the demand outlook with one supplier. Off
  unless agreed (north star safeguard: data shared outside the company only as agreed with that
  partner); ending it keeps the record of when it was on.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.common.models import AuditedModel


class PartnerAccess(models.Model):
    """The business partner a portal user acts for (one user, one partner, one company code)."""

    class Kind(models.TextChoices):
        SUPPLIER = "SUPPLIER", "Supplier"
        CUSTOMER = "CUSTOMER", "Customer"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="partner_access")
    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    partner = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="portal_users")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.user.username} -> {self.partner.number} ({self.kind})"


class OrderConfirmation(models.Model):
    """A supplier's confirmed quantity and date for one purchase order line; the latest one counts."""

    order_item = models.ForeignKey("purchasing.PurchaseOrderItem", on_delete=models.PROTECT,
                                   related_name="confirmations")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    delivery_date = models.DateField(help_text="The date the supplier will deliver.")
    note = models.CharField(max_length=200, blank=True, default="")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["order_item", "-confirmed_at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="confirmed_quantity_positive")]

    def __str__(self):
        return f"{self.order_item_id}: {self.quantity} on {self.delivery_date}"


class DeliveryNotice(AuditedModel):
    """An advance shipping notice from a supplier against one released purchase order."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    notice_number = models.CharField(max_length=12, help_text="Our number (AS...).")
    supplier = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=35, help_text="The supplier's delivery note number.")
    purchase_order = models.ForeignKey("purchasing.PurchaseOrder", on_delete=models.PROTECT,
                                       related_name="delivery_notices")
    ship_date = models.DateField()
    arrival_date = models.DateField()

    class Meta:
        ordering = ["-arrival_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "notice_number"], name="unique_notice_number"),
            models.UniqueConstraint(fields=["company_code", "supplier", "reference"], name="one_notice_per_reference"),
        ]

    def __str__(self):
        return f"{self.notice_number} ({self.reference})"


class DeliveryNoticeItem(models.Model):
    """What a delivery notice says is on its way for one purchase order line."""

    notice = models.ForeignKey(DeliveryNotice, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey("purchasing.PurchaseOrderItem", on_delete=models.PROTECT,
                                   related_name="notice_items")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="notice_quantity_positive")]


class ForecastShare(models.Model):
    """Purchasing's agreement to share the demand outlook with one supplier (off unless agreed)."""

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    supplier = models.ForeignKey("masterdata.BusinessPartner", on_delete=models.PROTECT, related_name="+")
    active = models.BooleanField(default=True)
    agreed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    agreed_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company_code", "supplier"], name="one_share_per_supplier")]

    def __str__(self):
        return f"Outlook for {self.supplier_id}: {'on' if self.active else 'off'}"
