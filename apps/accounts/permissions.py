"""Role-based access control (RBAC) for the ERP modules.

Each module gets one permission class with two role lists:

* ``read_roles``  - may use safe methods (GET, HEAD, OPTIONS)
* ``write_roles`` - may also create, update and delete

Superusers and the ADMIN role always pass. The matrix follows section 3 of the
design document:

=================  ==========================  ===================================
Role               Writes                      Reads only
=================  ==========================  ===================================
Finance Manager    finance, supplier invoices  sales, inventory, purchasing
Accountant         finance, supplier invoices  sales, inventory, purchasing
Sales Rep          sales (orders, customers)   inventory (catalogue lookup)
Inventory Manager  inventory, goods receipts   purchase orders
Purchasing Officer purchasing, goods receipts  inventory, supplier invoices
HR Admin           hr                          -
Production Planner production master data     cost estimates, all master data
Viewer             -                           sales, inventory, purchasing
=================  ==========================  ===================================
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role


class RolePermission(BasePermission):
    """DRF permission driven by the user's role: read roles may use GET/HEAD/OPTIONS, write roles
    may also change data. Administrators and superusers may do everything.
    """

    read_roles: tuple = ()
    write_roles: tuple = ()
    read_all: bool = False  # any signed-in user may read (shared master data)
    message = "Your role does not allow this action in this module."

    def has_permission(self, request, view):
        """Allow the request if the user's role is in the read or write list for this method."""
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
        """Make a named permission class for one module, e.g. ``FinancePermission``."""
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
    read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.INVENTORY_MANAGER, Role.VIEWER),
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

# --- Wave 2c -------------------------------------------------------------------

# Goods receipts: the warehouse books what arrived; purchasing can too.
GoodsReceiptPermission = RolePermission.build(
    "GoodsReceiptPermission",
    write=(Role.INVENTORY_MANAGER, Role.PURCHASING_OFFICER),
    read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.VIEWER),
)

# Supplier invoices (accounts payable) and the GR/IR report: finance posts, purchasing can look.
SupplierInvoicePermission = RolePermission.build(
    "SupplierInvoicePermission",
    write=(Role.ACCOUNTANT, Role.FINANCE_MANAGER),
    read=(Role.PURCHASING_OFFICER, Role.VIEWER),
)

# --- Wave 3 --------------------------------------------------------------------

# Cost and profit centres are master data: the Finance Manager maintains them, everyone can read them.
ControllingPermission = RolePermission.build(
    "ControllingPermission",
    write=(Role.FINANCE_MANAGER,),
    read_all=True,
)

# Payroll runs: HR calculates and posts payroll; finance can post and reverse it too and reads it.
PayrollPermission = RolePermission.build(
    "PayrollPermission",
    write=(Role.HR_ADMIN, Role.FINANCE_MANAGER),
    read=(Role.ACCOUNTANT,),
)

# --- Wave 4a -------------------------------------------------------------------

# Management reports. Financial statements: finance only. Margins: finance and sales. Stock ageing:
# finance and logistics. Payroll cost: finance manager and HR (a cost centre with one person
# would reveal that person's pay). Refreshing the views: finance manager.
FinancialReportPermission = RolePermission.build(
    "FinancialReportPermission", read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT),
)
MarginReportPermission = RolePermission.build(
    "MarginReportPermission", read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.SALES_REP),
)
StockReportPermission = RolePermission.build(
    "StockReportPermission",
    read=(Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.INVENTORY_MANAGER, Role.PURCHASING_OFFICER),
)
PayrollReportPermission = RolePermission.build(
    "PayrollReportPermission", read=(Role.FINANCE_MANAGER, Role.HR_ADMIN),
)
AnalyticsAdminPermission = RolePermission.build(
    "AnalyticsAdminPermission", write=(Role.FINANCE_MANAGER,), read=(Role.ACCOUNTANT,),
)

# --- Wave 4b -------------------------------------------------------------------

# Knowledge graph: every signed-in user may query it, but each query only returns the node types
# their role may see (apps/graph/access.py). Rebuilding the graph is for administrators.
GraphPermission = RolePermission.build("GraphPermission", read_all=True)
GraphAdminPermission = RolePermission.build("GraphAdminPermission")

# --- Wave 5a -------------------------------------------------------------------

# Production master data (equipment hierarchy, BOMs, routings): the production planner maintains it,
# everyone can read it (sales and purchasing need to see what goes into a product).
ProductionPermission = RolePermission.build(
    "ProductionPermission", write=(Role.PRODUCTION_PLANNER,), read_all=True,
)

# Activity types and their rates are controlling data: the Finance Manager sets them.
ActivityRatePermission = RolePermission.build(
    "ActivityRatePermission", write=(Role.FINANCE_MANAGER,), read_all=True,
)

# Cost estimates reveal product costs and margins: planners, finance and the warehouse only.
CostEstimatePermission = RolePermission.build(
    "CostEstimatePermission",
    read=(Role.PRODUCTION_PLANNER, Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.INVENTORY_MANAGER),
)
