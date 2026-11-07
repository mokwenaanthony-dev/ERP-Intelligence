"""Ontology v1: how ERP records become nodes and relationships.

Every projector reads the *current* state of one record and writes it with idempotent MERGEs, so
events can be processed more than once or out of order and the graph still ends up correct; the
whole graph can also be rebuilt from scratch at any time (``graph_rebuild``).

Nodes are keyed by business keys, the same numbers people and other systems use (partner and
material numbers, plant codes, "company/document number"), so an MES or another system can join the
graph without mapping tables.

    (:BusinessPartner:Customer)-[:PLACED]->(:SalesOrder)-[:CONTAINS]->(:Material)
    (:SalesOrder)-[:DELIVERED_BY]->(:Delivery)-[:BILLED_BY]->(:Invoice)-[:PAID_BY]->(:Payment)
    (:PurchaseOrder)-[:ORDERED_FROM]->(:BusinessPartner:Supplier)-[:SUPPLIES]->(:Material)
    (:PurchaseOrder)-[:RECEIVED_BY]->(:GoodsReceipt), (:PurchaseOrder)-[:BILLED_BY]->(:SupplierInvoice)
    (:Employee)-[:HOLDS]->(:Position)-[:IN]->(:OrgUnit)-[:CHARGED_TO]->(:CostCenter)-[:BELONGS_TO]->(:ProfitCenter)

Pay data never enters the graph: employees carry name, dates and position only.
"""
from django.apps import apps

DOCUMENT_LABELS = ["SalesOrder", "Delivery", "Invoice", "PurchaseOrder", "GoodsReceipt", "SupplierInvoice",
                   "Payment"]
MASTER_LABELS = ["CompanyCode", "BusinessPartner", "Material", "Plant", "ProfitCenter", "CostCenter", "OrgUnit",
                 "Position", "Employee"]
ALL_LABELS = MASTER_LABELS + DOCUMENT_LABELS


def _doc_key(document, number_field):
    return f"{document.company_code.code}/{getattr(document, number_field)}"


def _company_child_key(obj):
    return f"{obj.company_code.code}/{obj.code}"


# --- master data -----------------------------------------------------------------------------

def company_code(g, c):
    g.node("CompanyCode", c.code, {"name": c.name, "currency": c.currency.code})


def business_partner(g, bp):
    extra = []
    for role in bp.customer_roles.select_related("company_code"):
        extra.append("Customer")
        g.edge("BusinessPartner", bp.number, "CUSTOMER_OF", "CompanyCode", role.company_code.code,
               {"payment_terms_days": role.payment_terms_days, "credit_limit": role.credit_limit})
    for role in bp.supplier_roles.select_related("company_code"):
        extra.append("Supplier")
        g.edge("BusinessPartner", bp.number, "SUPPLIER_OF", "CompanyCode", role.company_code.code,
               {"payment_terms_days": role.payment_terms_days})
    g.node("BusinessPartner", bp.number, {
        "name": bp.name, "category": bp.category, "city": bp.city, "country": bp.country,
        "vat_number": bp.vat_number, "is_blocked": bp.is_blocked,
    }, extra_labels=extra)


def partner_role(g, role):
    business_partner(g, role.business_partner)


def material(g, m):
    g.node("Material", m.number, {"description": m.description, "material_type": m.material_type,
                                  "is_active": m.is_active})


def plant(g, p):
    g.node("Plant", p.code, {"name": p.name})
    g.edge("Plant", p.code, "BELONGS_TO", "CompanyCode", p.company_code.code)
    g.clear_edges("Plant", p.code, "ASSIGNED_TO")
    link = getattr(p, "profit_center_link", None)  # None when the plant has no profit centre
    if link is not None:
        g.edge("Plant", p.code, "ASSIGNED_TO", "ProfitCenter", _company_child_key(link.profit_center))


def plant_profit_center(g, link):
    plant(g, link.plant)


def profit_center(g, pc):
    key = _company_child_key(pc)
    g.node("ProfitCenter", key, {"code": pc.code, "name": pc.name, "is_active": pc.is_active})
    g.edge("ProfitCenter", key, "BELONGS_TO", "CompanyCode", pc.company_code.code)


def cost_center(g, cc):
    key = _company_child_key(cc)
    g.node("CostCenter", key, {"code": cc.code, "name": cc.name, "category": cc.category, "is_active": cc.is_active})
    g.clear_edges("CostCenter", key, "BELONGS_TO")
    g.edge("CostCenter", key, "BELONGS_TO", "ProfitCenter", _company_child_key(cc.profit_center))


def org_unit(g, unit):
    key = _company_child_key(unit)
    g.node("OrgUnit", key, {"code": unit.code, "name": unit.name})
    for rel in ("PART_OF", "CHARGED_TO", "BELONGS_TO"):
        g.clear_edges("OrgUnit", key, rel)
    g.edge("OrgUnit", key, "BELONGS_TO", "CompanyCode", unit.company_code.code)
    if unit.parent_id:
        g.edge("OrgUnit", key, "PART_OF", "OrgUnit", _company_child_key(unit.parent))
    if unit.cost_center_id:
        g.edge("OrgUnit", key, "CHARGED_TO", "CostCenter", _company_child_key(unit.cost_center))


def _position_key(position):
    return f"{_company_child_key(position.org_unit)}/{position.code}"


def position(g, pos):
    key = _position_key(pos)
    g.node("Position", key, {"code": pos.code, "title": pos.title})
    g.clear_edges("Position", key, "IN")
    g.clear_edges("Position", key, "CHARGED_TO")
    g.edge("Position", key, "IN", "OrgUnit", _company_child_key(pos.org_unit))
    if pos.cost_center_id:
        g.edge("Position", key, "CHARGED_TO", "CostCenter", _company_child_key(pos.cost_center))


def employee(g, e):
    key = f"{e.company_code.code}/{e.personnel_number}"
    # Deliberately no salary or tax reference: pay data stays in the ERP, behind HR permissions.
    g.node("Employee", key, {"personnel_number": e.personnel_number, "name": e.full_name,
                             "hire_date": e.hire_date, "termination_date": e.termination_date})
    g.clear_edges("Employee", key, "HOLDS")
    g.edge("Employee", key, "HOLDS", "Position", _position_key(e.position))
    g.edge("Employee", key, "EMPLOYED_BY", "CompanyCode", e.company_code.code)


# --- order to cash ---------------------------------------------------------------------------

def sales_order(g, order):
    key = _doc_key(order, "order_number")
    g.node("SalesOrder", key, {"number": order.order_number, "date": order.order_date, "status": order.status,
                               "net_total": order.net_total, "customer_reference": order.customer_reference})
    g.edge("BusinessPartner", order.customer.number, "PLACED", "SalesOrder", key)
    g.clear_edges("SalesOrder", key, "CONTAINS")
    for item in order.items.select_related("material"):
        g.edge("SalesOrder", key, "CONTAINS", "Material", item.material.number,
               {"quantity": item.quantity, "net_price": item.net_price}, merge_on={"item_number": item.item_number})


def delivery(g, d):
    key = _doc_key(d, "delivery_number")
    g.node("Delivery", key, {"number": d.delivery_number, "date": d.delivery_date, "status": d.status})
    g.edge("SalesOrder", _doc_key(d.sales_order, "order_number"), "DELIVERED_BY", "Delivery", key)
    g.edge("Delivery", key, "SHIPPED_FROM", "Plant", d.plant.code)
    g.clear_edges("Delivery", key, "SHIPS")
    for item in d.items.select_related("order_item__material"):
        g.edge("Delivery", key, "SHIPS", "Material", item.order_item.material.number, {"quantity": item.quantity},
               merge_on={"item_number": item.order_item.item_number})


def invoice(g, inv):
    key = _doc_key(inv, "invoice_number")
    g.node("Invoice", key, {"number": inv.invoice_number, "date": inv.invoice_date, "due_date": inv.due_date,
                            "net_amount": inv.net_amount, "tax_amount": inv.tax_amount,
                            "gross_amount": inv.gross_amount, "status": inv.status})
    g.edge("Delivery", _doc_key(inv.delivery, "delivery_number"), "BILLED_BY", "Invoice", key)
    g.edge("Invoice", key, "BILLED_TO", "BusinessPartner", inv.customer.number)


# --- procure to pay --------------------------------------------------------------------------

def purchase_order(g, order):
    key = _doc_key(order, "order_number")
    g.node("PurchaseOrder", key, {"number": order.order_number, "date": order.order_date, "status": order.status,
                                  "net_total": order.net_total})
    g.edge("PurchaseOrder", key, "ORDERED_FROM", "BusinessPartner", order.supplier.number)
    g.clear_edges("PurchaseOrder", key, "CONTAINS")
    for item in order.items.select_related("material"):
        g.edge("PurchaseOrder", key, "CONTAINS", "Material", item.material.number,
               {"quantity": item.quantity, "net_price": item.net_price}, merge_on={"item_number": item.item_number})
        if order.status != "CANCELLED":
            g.edge("BusinessPartner", order.supplier.number, "SUPPLIES", "Material", item.material.number)


def goods_receipt(g, receipt):
    key = _doc_key(receipt, "receipt_number")
    g.node("GoodsReceipt", key, {"number": receipt.receipt_number, "date": receipt.receipt_date,
                                 "status": receipt.status, "delivery_note": receipt.delivery_note})
    g.edge("PurchaseOrder", _doc_key(receipt.purchase_order, "order_number"), "RECEIVED_BY", "GoodsReceipt", key)
    g.edge("GoodsReceipt", key, "RECEIVED_AT", "Plant", receipt.plant.code)


def supplier_invoice(g, inv):
    key = _doc_key(inv, "invoice_number")
    g.node("SupplierInvoice", key, {"number": inv.invoice_number, "supplier_reference": inv.supplier_reference,
                                    "date": inv.invoice_date, "due_date": inv.due_date,
                                    "gross_amount": inv.gross_amount, "status": inv.status})
    g.edge("PurchaseOrder", _doc_key(inv.purchase_order, "order_number"), "BILLED_BY", "SupplierInvoice", key)
    g.edge("SupplierInvoice", key, "BILLED_BY_SUPPLIER", "BusinessPartner", inv.supplier.number)


# --- payments (customer DZ and supplier KZ journal entries) ----------------------------------

PAYMENT_TYPES = {"DZ": "INCOMING", "KZ": "OUTGOING"}


def payment_key(entry):
    return f"{entry.company_code.code}/{entry.fiscal_year}/{entry.document_number}"


def journal_entry(g, entry):
    if entry.reversal_of_id:  # a reversal changes the status of the payment it reverses
        entry = entry.reversal_of
    if entry.document_type not in PAYMENT_TYPES:
        return
    Invoice = apps.get_model("sales", "Invoice")
    SupplierInvoice = apps.get_model("purchasing", "SupplierInvoice")
    key = payment_key(entry)
    clearings = list(entry.clearings_made.select_related("item"))
    partner = next((line.business_partner for line in entry.lines.select_related("business_partner")
                    if line.business_partner_id), None)
    g.node("Payment", key, {"number": entry.document_number, "date": entry.posting_date, "status": entry.status,
                            "direction": PAYMENT_TYPES[entry.document_type], "reference": entry.reference,
                            "amount": sum((c.amount for c in clearings), 0)})
    if partner is not None:
        if entry.document_type == "DZ":
            g.edge("BusinessPartner", partner.number, "PAID", "Payment", key)
        else:
            g.edge("Payment", key, "PAID_TO", "BusinessPartner", partner.number)
    for clearing in clearings:
        entry_id = clearing.item.entry_id
        for model, label, field in ((Invoice, "Invoice", "invoice_number"),
                                    (SupplierInvoice, "SupplierInvoice", "invoice_number")):
            for doc in model.objects.filter(journal_entry_id=entry_id).select_related("company_code"):
                g.edge(label, _doc_key(doc, field), "PAID_BY", "Payment", key, {"amount": clearing.amount})


# --- registry --------------------------------------------------------------------------------

# "app_label.model" -> (projector, label and key of the node it owns, for deletions; None = no own node)
PROJECTORS = {
    "organization.companycode": (company_code, lambda o: ("CompanyCode", o.code)),
    "organization.plant": (plant, lambda o: ("Plant", o.code)),
    "masterdata.businesspartner": (business_partner, lambda o: ("BusinessPartner", o.number)),
    "masterdata.customerrole": (partner_role, None),
    "masterdata.supplierrole": (partner_role, None),
    "masterdata.material": (material, lambda o: ("Material", o.number)),
    "controlling.profitcenter": (profit_center, lambda o: ("ProfitCenter", _company_child_key(o))),
    "controlling.costcenter": (cost_center, lambda o: ("CostCenter", _company_child_key(o))),
    "controlling.plantprofitcenter": (plant_profit_center, None),
    "hr.orgunit": (org_unit, lambda o: ("OrgUnit", _company_child_key(o))),
    "hr.position": (position, lambda o: ("Position", _position_key(o))),
    "hr.employee": (employee, lambda o: ("Employee", f"{o.company_code.code}/{o.personnel_number}")),
    "sales.salesorder": (sales_order, lambda o: ("SalesOrder", _doc_key(o, "order_number"))),
    "sales.delivery": (delivery, lambda o: ("Delivery", _doc_key(o, "delivery_number"))),
    "sales.invoice": (invoice, lambda o: ("Invoice", _doc_key(o, "invoice_number"))),
    "purchasing.purchaseorder": (purchase_order, lambda o: ("PurchaseOrder", _doc_key(o, "order_number"))),
    "purchasing.goodsreceipt": (goods_receipt, lambda o: ("GoodsReceipt", _doc_key(o, "receipt_number"))),
    "purchasing.supplierinvoice": (supplier_invoice, lambda o: ("SupplierInvoice", _doc_key(o, "invoice_number"))),
    "finance.journalentry": (journal_entry, None),
}


def model_key(instance) -> str:
    return f"{instance._meta.app_label}.{instance._meta.model_name}"


def project_instance(g, instance):
    projector, _ = PROJECTORS[model_key(instance)]
    projector(g, instance)


def project(g, aggregate_type, aggregate_id, payload=None):
    """Project one changed record. Returns False if it no longer exists (nothing to do)."""
    if payload and payload.get("deleted"):
        g.delete_node(payload["label"], payload["key"])
        return True
    model = apps.get_model(aggregate_type)
    instance = model.objects.filter(pk=aggregate_id).first()
    if instance is None:
        return False
    PROJECTORS[aggregate_type][0](g, instance)
    return True


def rebuild(g, batch_size=200) -> int:
    """Project every record of every registered model, master data first. Returns the count."""
    count = 0
    for aggregate_type, (projector, _) in PROJECTORS.items():
        if aggregate_type in ("masterdata.customerrole", "masterdata.supplierrole", "controlling.plantprofitcenter"):
            continue  # projected through their business partner or plant
        model = apps.get_model(aggregate_type)
        queryset = model.objects.all()
        if aggregate_type == "finance.journalentry":
            queryset = queryset.filter(document_type__in=list(PAYMENT_TYPES), reversal_of__isnull=True)
        for instance in queryset.iterator(chunk_size=batch_size):
            projector(g, instance)
            count += 1
            if count % batch_size == 0:
                g.flush()
    g.flush()
    return count
