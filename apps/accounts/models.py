"""Custom user model with an ERP role.

AUTH_USER_MODEL points here, so this model must exist before the first
`manage.py migrate`. Swapping the user model later needs a database rebuild.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Administrator"
    FINANCE_MANAGER = "FINANCE_MANAGER", "Finance Manager"
    ACCOUNTANT = "ACCOUNTANT", "Accountant"
    SALES_REP = "SALES_REP", "Sales Representative"
    INVENTORY_MANAGER = "INVENTORY_MANAGER", "Inventory Manager"
    PURCHASING_OFFICER = "PURCHASING_OFFICER", "Purchasing Officer"
    HR_ADMIN = "HR_ADMIN", "HR Admin"
    PRODUCTION_PLANNER = "PRODUCTION_PLANNER", "Production Planner"
    VIEWER = "VIEWER", "Viewer"
    # Outside the company (Wave 10a): a person at a supplier or a customer, linked to that business
    # partner. They use only /api/portal/ and see only their own documents (ADR 0019).
    SUPPLIER = "SUPPLIER", "Supplier (portal)"
    CUSTOMER = "CUSTOMER", "Customer (portal)"


# Roles of people outside the company: refused by every internal permission (apps.accounts.permissions).
PARTNER_ROLES = frozenset({Role.SUPPLIER, Role.CUSTOMER})


class User(AbstractUser):
    """An ERP user. ``role`` decides what each module's API lets them read and change."""

    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.VIEWER,
        help_text="Drives module-level permissions across the ERP.",
    )

    def has_role(self, *roles: str) -> bool:
        """True if the user has any of the given roles, e.g. ``user.has_role(Role.HR_ADMIN)``."""
        return self.role in roles

    def __str__(self) -> str:
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"
