"""Which node types each role may see in the graph: the API's role rules, carried over.

Every graph query is filtered to these labels, including the KG-RAG context an AI agent receives,
so an agent acting for a user can never see more than that user could through the API.
"""
from apps.accounts.models import Role

from .projection import ALL_LABELS

# Production master data (equipment, BOMs, routings) is visible to everyone, as in the API.
BASE = {"CompanyCode", "Plant", "Material", "ProfitCenter", "CostCenter", "Area", "Line", "Cell", "BOM", "Routing"}
SALES = {"BusinessPartner", "SalesOrder", "Delivery", "Invoice", "Payment"}
PURCHASING = {"BusinessPartner", "PurchaseOrder", "GoodsReceipt", "SupplierInvoice"}
PEOPLE = {"OrgUnit", "Position", "Employee"}
FINANCE = SALES | PURCHASING | {"ProductionOrder"}

ROLE_LABELS = {
    Role.FINANCE_MANAGER: BASE | FINANCE,
    Role.ACCOUNTANT: BASE | FINANCE,
    Role.SALES_REP: BASE | SALES,
    Role.PURCHASING_OFFICER: BASE | PURCHASING,
    Role.INVENTORY_MANAGER: BASE | {"BusinessPartner", "SalesOrder", "Delivery", "PurchaseOrder", "GoodsReceipt",
                                    "ProductionOrder"},
    Role.HR_ADMIN: BASE | PEOPLE,
    Role.PRODUCTION_PLANNER: BASE | {"BusinessPartner", "SalesOrder", "PurchaseOrder", "GoodsReceipt",
                                     "ProductionOrder"},
    Role.VIEWER: BASE | {"BusinessPartner", "SalesOrder", "Delivery", "PurchaseOrder", "GoodsReceipt",
                         "ProductionOrder"},
}


def allowed_labels(user) -> set:
    """Node labels this user may see in graph results, mirroring their API permissions."""
    if user.is_superuser or user.role == Role.ADMIN:
        return set(ALL_LABELS)
    return set(ROLE_LABELS.get(user.role, BASE))
