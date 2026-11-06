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
    message = "Your role does not allow this action in this module."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or user.role == Role.ADMIN:
            return True
        if request.method in SAFE_METHODS:
            return user.role in self.read_roles or user.role in self.write_roles
        return user.role in self.write_roles

    @classmethod
    def build(cls, name: str, *, read=(), write=()):
        return type(name, (cls,), {"read_roles": tuple(read), "write_roles": tuple(write)})


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
