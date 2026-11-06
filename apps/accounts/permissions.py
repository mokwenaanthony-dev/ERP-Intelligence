"""Role-based access control (RBAC) for the ERP modules.

Each module gets one permission class with two role lists:

* ``read_roles``  - may use safe methods (GET, HEAD, OPTIONS)
* ``write_roles`` - may also create, update and delete

Superusers and the ADMIN role always pass. The matrix follows section 3 of the
design document:

=================  ==========================  ===================================
Role               Writes                      Reads only
=================  ==========================  ===================================
Finance Manager    finance                     sales, inventory
Accountant         finance                     sales, inventory
Sales Rep          sales (orders, customers)   inventory (catalogue lookup)
Inventory Manager  inventory                   -
Purchasing Officer purchasing                  inventory
HR Admin           hr                          -
Viewer             -                           sales, inventory
=================  ==========================  ===================================
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role


class RolePermission(BasePermission):
    read_roles: tuple = ()
    write_roles: tuple = ()
    read_all: bool = False  # any signed-in user may read (shared master data)
    message = "Your role does not allow this action in this module."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or user.role == Role.ADMIN:
            return True
        if request.method in SAFE_METHODS:
            return self.read_all or user.role in self.read_roles or user.role in self.write_roles
        return user.role in self.write_roles

    @classmethod
    def build(cls, name: str, *, read=(), write=(), read_all=False):
        attrs = {"read_roles": tuple(read), "write_roles": tuple(write), "read_all": read_all}
        return type(name, (cls,), attrs)


FinancePermission = RolePermission.build(
    "FinancePermission",
    write=(Role.FINANCE_MANAGER, Role.ACCOUNTANT),
)

SalesPermission = RolePermission.build(
    "SalesPermission",
    write=(Role.SALES_REP,),
    read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.VIEWER),
)

InventoryPermission = RolePermission.build(
    "InventoryPermission",
    write=(Role.INVENTORY_MANAGER,),
    read=(Role.SALES_REP, Role.PURCHASING_OFFICER, Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.VIEWER),
)

PurchasingPermission = RolePermission.build(
    "PurchasingPermission",
    write=(Role.PURCHASING_OFFICER,),
    read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT),
)

HRPermission = RolePermission.build(
    "HRPermission",
    write=(Role.HR_ADMIN,),
)

# --- Wave 1 -------------------------------------------------------------------

# Organisation structure is configured by administrators; everyone can read it.
OrganizationPermission = RolePermission.build("OrganizationPermission", read_all=True)

# Currencies, exchange rates and units of measure.
ReferenceDataPermission = RolePermission.build(
    "ReferenceDataPermission",
    write=(Role.FINANCE_MANAGER,),
    read_all=True,
)

BusinessPartnerPermission = RolePermission.build(
    "BusinessPartnerPermission",
    write=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.SALES_REP, Role.PURCHASING_OFFICER),
    read_all=True,
)

MaterialPermission = RolePermission.build(
    "MaterialPermission",
    write=(Role.INVENTORY_MANAGER, Role.PURCHASING_OFFICER),
    read_all=True,
)

# Chart of accounts, G/L accounts, tax codes, fiscal years and period open/close.
FinanceConfigPermission = RolePermission.build(
    "FinanceConfigPermission",
    write=(Role.FINANCE_MANAGER,),
    read=(Role.ACCOUNTANT,),
)

# --- Wave 2b -------------------------------------------------------------------

# Deliveries are created from sales orders; the warehouse can also reverse them.
DeliveryPermission = RolePermission.build(
    "DeliveryPermission",
    write=(Role.SALES_REP, Role.INVENTORY_MANAGER),
    read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.VIEWER),
)

# Billing: sales raises invoices; finance can raise and cancel them too.
BillingPermission = RolePermission.build(
    "BillingPermission",
    write=(Role.SALES_REP, Role.ACCOUNTANT, Role.FINANCE_MANAGER),
    read=(Role.VIEWER,),
)
