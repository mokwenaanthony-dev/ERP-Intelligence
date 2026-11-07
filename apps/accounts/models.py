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
    VIEWER = "VIEWER", "Viewer"


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
