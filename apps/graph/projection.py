"""Ontology v1 (version 1.5): how ERP records become nodes and relationships.

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
    (:Cell)-[:PART_OF]->(:Line)-[:PART_OF]->(:Area)-[:PART_OF]->(:Plant), (:Cell)-[:CHARGED_TO]->(:CostCenter)
    (:BOM)-[:FOR]->(:Material), (:BOM)-[:CONSUMES]->(:Material), (:Routing)-[:HAS_OPERATION]->(:Cell)
    (:ProductionOrder)-[:PRODUCES]->(:Material), (:ProductionOrder)-[:CONSUMES]->(:Material),
    (:ProductionOrder)-[:HAS_OPERATION]->(:Cell), (:Cell)-[:RUNNING]->(:ProductionOrder)
    (:BusinessPartner)-[:HAS_OPPORTUNITY]->(:Opportunity)-[:FROM_CAMPAIGN]->(:Campaign)
    (:Opportunity)-[:QUOTED_IN]->(:Quotation)-[:ORDERED_AS]->(:SalesOrder)
    (:Quotation)-[:QUOTED_TO]->(:BusinessPartner)
    (:BusinessPartner)-[:RAISED]->(:ServiceTicket)-[:ABOUT]->(:SalesOrder | :Material)
    (:RFQ)-[:REQUESTS]->(:Material), (:RFQ)-[:INVITED]->(:BusinessPartner), (:RFQ)-[:AWARDED_AS]->(:PurchaseOrder)
    (:Contract)-[:WITH]->(:BusinessPartner), (:Contract)-[:COVERS]->(:Material)
    (:Contract)-[:RELEASED_AS]->(:PurchaseOrder)

Since 1.3 (Wave 6d) a ``Cell`` also carries its live state from the shop floor (``state``,
``state_since``, ``state_order``, ``state_operation``, ``state_reported_at``) and, while running, a
``RUNNING`` relationship to the order it works on. The state comes from ``production.CellState``, the
ERP's copy of the cell's retained UNS state, so the graph is still rebuilt from ERP tables only.

Equipment nodes use ISA-95 names (ADR 0006) and carry their ISA-95 level and Unified Namespace topic
path (``uns_path``). The full ontology, with every property, is documented in ``docs/ONTOLOGY.md``.

Since 1.4 (Wave 7a, CRM) campaigns, opportunities, quotations and service tickets are nodes too.
Leads are not: they hold a contact's personal data and are not customers yet (ADR 0012). Ticket
descriptions and resolutions stay out as well (free text that may name people); a ticket carries its
number, subject, priority, status and times.

Since 1.5 (Wave 7b, SRM) requests for quotation and contracts are nodes, and a supplier's
``BusinessPartner`` node carries its latest evaluation (``supplier_grade``, ``supplier_score``,
``supplier_evaluated_to``). Supplier applications are not projected: they hold an applicant's
personal data and the company is not a supplier yet (ADR 0013).

Pay data never enters the graph: employees carry name, dates and position only.
"""
from django.apps import apps

DOCUMENT_LABELS = ["Quotation", "SalesOrder", "Delivery", "Invoice", "RFQ", "Contract", "PurchaseOrder",
                   "GoodsReceipt", "SupplierInvoice", "Payment", "ProductionOrder", "Opportunity", "ServiceTicket"]
MASTER_LABELS = ["CompanyCode", "BusinessPartner", "Material", "Plant", "ProfitCenter", "CostCenter", "OrgUnit",
                 "Position", "Employee", "Area", "Line", "Cell", "BOM", "Routing", "Campaign"]
ONTOLOGY_VERSION = "1.5"  # bump with docs/ONTOLOGY.md; a breaking change needs `graph_rebuild --clear`
ALL_LABELS = MASTER_LABELS + DOCUMENT_LABELS


def _doc_key(document, number_field):
    return f"{document.company_code.code}/{getattr(document, number_field)}"


def _company_child_key(obj):
    return f"{obj.company_code.code}/{obj.code}"


# Each projector below takes the store ``g`` and one ERP record, and writes that record's node and
# its outgoing relationships. ``PROJECTORS`` at the bottom maps each model to its projector.

# --- master data -----------------------------------------------------------------------------

def company_code(g, c):
    """``CompanyCode`` node: name and currency."""
    g.node("CompanyCode", c.code, {"name": c.name, "currency": c.currency.code})


def business_partner(g, bp):
    """``BusinessPartner`` node, labelled ``Customer`` and/or ``Supplier`` by its roles, with
    CUSTOMER_OF / SUPPLIER_OF edges to each company code (payment terms, credit limit).
    """
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
    """A customer or supplier role changed: re-project the partner that owns it."""
    business_partner(g, role.business_partner)


def material(g, m):
    """``Material`` node: description, material type and whether it is active."""
    g.node("Material", m.number, {"description": m.description, "material_type": m.material_type,
                                  "is_active": m.is_active})


def plant(g, p):
    """``Plant`` node, BELONGS_TO its company code and ASSIGNED_TO its default profit centre."""
    g.node("Plant", p.code, {"name": p.name})
    g.edge("Plant", p.code, "BELONGS_TO", "CompanyCode", p.company_code.code)
    g.clear_edges("Plant", p.code, "ASSIGNED_TO")
    link = getattr(p, "profit_center_link", None)  # None when the plant has no profit centre
    if link is not None:
        g.edge("Plant", p.code, "ASSIGNED_TO", "ProfitCenter", _company_child_key(link.profit_center))


def plant_profit_center(g, link):
    """A plant's profit centre assignment changed: re-project the plant."""
    plant(g, link.plant)


def profit_center(g, pc):
    """``ProfitCenter`` node, BELONGS_TO its company code."""
    key = _company_child_key(pc)
    g.node("ProfitCenter", key, {"code": pc.code, "name": pc.name, "is_active": pc.is_active})
    g.edge("ProfitCenter", key, "BELONGS_TO", "CompanyCode", pc.company_code.code)


def cost_center(g, cc):
    """``CostCenter`` node, BELONGS_TO its profit centre."""
    key = _company_child_key(cc)
    g.node("CostCenter", key, {"code": cc.code, "name": cc.name, "category": cc.category, "is_active": cc.is_active})
    g.clear_edges("CostCenter", key, "BELONGS_TO")
    g.edge("CostCenter", key, "BELONGS_TO", "ProfitCenter", _company_child_key(cc.profit_center))


def org_unit(g, unit):
    """``OrgUnit`` node: BELONGS_TO the company, PART_OF its parent unit, CHARGED_TO its cost centre."""
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
    """``Position`` node: IN its org unit, CHARGED_TO its own cost centre if it has one."""
    key = _position_key(pos)
    g.node("Position", key, {"code": pos.code, "title": pos.title})
    g.clear_edges("Position", key, "IN")
    g.clear_edges("Position", key, "CHARGED_TO")
    g.edge("Position", key, "IN", "OrgUnit", _company_child_key(pos.org_unit))
    if pos.cost_center_id:
        g.edge("Position", key, "CHARGED_TO", "CostCenter", _company_child_key(pos.cost_center))


def employee(g, e):
    """``Employee`` node (name and dates, never pay data): HOLDS a position, EMPLOYED_BY the company."""
    key = f"{e.company_code.code}/{e.personnel_number}"
    # Deliberately no salary or tax reference: pay data stays in the ERP, behind HR permissions.
    g.node("Employee", key, {"personnel_number": e.personnel_number, "name": e.full_name,
                             "hire_date": e.hire_date, "termination_date": e.termination_date})
    g.clear_edges("Employee", key, "HOLDS")
    g.edge("Employee", key, "HOLDS", "Position", _position_key(e.position))
    g.edge("Employee", key, "EMPLOYED_BY", "CompanyCode", e.company_code.code)


# --- production master data (ISA-95 equipment hierarchy, BOMs, routings) ----------------------

def _plant_child_key(obj):
    return f"{obj.plant.code}/{obj.code}"


def production_area(g, area):
    """``Area`` node (ISA-95 area) with its UNS path, PART_OF its plant."""
    key = _plant_child_key(area)
    g.node("Area", key, {"code": area.code, "name": area.name, "isa95_level": area.isa95_level,
                         "uns_path": area.uns_path, "is_active": area.is_active})
    g.edge("Area", key, "PART_OF", "Plant", area.plant.code)


def production_line(g, line):
    """``Line`` node (ISA-95 work centre of type production line), PART_OF its area."""
    key = _plant_child_key(line)
    g.node("Line", key, {"code": line.code, "name": line.name, "isa95_level": line.isa95_level,
                         "isa95_type": line.isa95_type, "uns_path": line.uns_path, "is_active": line.is_active})
    g.edge("Line", key, "PART_OF", "Area", _plant_child_key(line.area))


def work_center(g, wc):
    """``Cell`` node (the ERP's work centre, an ISA-95 work cell): PART_OF its line, CHARGED_TO
    the cost centre its activity is costed through, its live state from the shop floor, and RUNNING
    the production order it is working on (Wave 6d).
    """
    from apps.production.models import CellState, ProductionOrder

    key = _plant_child_key(wc)
    live = CellState.objects.filter(work_center=wc).first()
    # Labelled "Cell" (ISA-95), although the ERP calls it a work centre (SAP): see ADR 0006.
    # State properties set to None are removed, so a cell without a report shows no stale state.
    g.node("Cell", key, {"code": wc.code, "name": wc.name, "isa95_level": wc.isa95_level,
                         "uns_path": wc.uns_path, "is_active": wc.is_active,
                         "capacity_hours_per_day": wc.capacity_hours_per_day,
                         "activity_types": sorted(a.code for a in wc.activity_types.all()),
                         "state": live.state if live else None,
                         "state_since": live.since if live else None,
                         "state_order": (live.order_number or None) if live else None,
                         "state_operation": live.operation_number if live else None,
                         "state_reported_at": live.reported_at if live else None})
    for rel in ("PART_OF", "CHARGED_TO", "RUNNING"):
        g.clear_edges("Cell", key, rel)
    g.edge("Cell", key, "PART_OF", "Line", _plant_child_key(wc.line))
    g.edge("Cell", key, "CHARGED_TO", "CostCenter", _company_child_key(wc.cost_center))
    if live and live.state == CellState.State.RUNNING and live.order_number:
        # Only to an order the ERP has: the edge never creates an order node out of a message.
        order = ProductionOrder.objects.filter(company_code=wc.plant.company_code,
                                               order_number=live.order_number).first()
        if order is not None:
            g.edge("Cell", key, "RUNNING", "ProductionOrder", _doc_key(order, "order_number"),
                   {"operation_number": live.operation_number, "since": live.since})


def cell_state(g, state):
    """A cell's live state changed: project its ``Cell`` node again (the state has no node of its own)."""
    work_center(g, state.work_center)


def bill_of_materials(g, bom):
    """``BOM`` node: FOR the product it makes, CONSUMES each component (quantity per base quantity)."""
    g.node("BOM", bom.key, {"material": bom.material.number, "plant": bom.plant.code, "valid_from": bom.valid_from,
                            "base_quantity": bom.base_quantity, "status": bom.status})
    g.edge("BOM", bom.key, "FOR", "Material", bom.material.number)
    g.clear_edges("BOM", bom.key, "CONSUMES")
    for item in bom.items.select_related("component"):
        g.edge("BOM", bom.key, "CONSUMES", "Material", item.component.number, {"quantity": item.quantity},
               merge_on={"item_number": item.item_number})


def routing(g, r):
    """``Routing`` node: FOR the product, HAS_OPERATION to the cell of each operation."""
    g.node("Routing", r.key, {"material": r.material.number, "plant": r.plant.code, "valid_from": r.valid_from,
                              "base_quantity": r.base_quantity, "status": r.status})
    g.edge("Routing", r.key, "FOR", "Material", r.material.number)
    g.clear_edges("Routing", r.key, "HAS_OPERATION")
    for op in r.operations.select_related("work_center__plant"):
        g.edge("Routing", r.key, "HAS_OPERATION", "Cell", _plant_child_key(op.work_center),
               {"description": op.description}, merge_on={"operation_number": op.operation_number})


def production_order(g, order):
    """``ProductionOrder`` node: PRODUCES the product, MADE_AT the plant, CONSUMES its components
    (quantity for the whole order) and HAS_OPERATION to the cell of each operation.
    """
    key = _doc_key(order, "order_number")
    g.node("ProductionOrder", key, {
        "number": order.order_number, "status": order.status, "quantity": order.quantity,
        "start_date": order.start_date, "finish_date": order.finish_date, "planned_cost": order.planned_cost,
    })
    g.edge("ProductionOrder", key, "PRODUCES", "Material", order.material.number)
    g.edge("ProductionOrder", key, "MADE_AT", "Plant", order.plant.code)
    for rel in ("CONSUMES", "HAS_OPERATION"):
        g.clear_edges("ProductionOrder", key, rel)
    for component in order.components.select_related("material"):
        g.edge("ProductionOrder", key, "CONSUMES", "Material", component.material.number,
               {"quantity": component.required_quantity}, merge_on={"item_number": component.item_number})
    for op in order.operations.select_related("work_center__plant"):
        g.edge("ProductionOrder", key, "HAS_OPERATION", "Cell", _plant_child_key(op.work_center),
               {"description": op.description}, merge_on={"operation_number": op.operation_number})


# --- order to cash ---------------------------------------------------------------------------

def sales_order(g, order):
    """``SalesOrder`` node: PLACED by the customer, CONTAINS each ordered material."""
    key = _doc_key(order, "order_number")
    g.node("SalesOrder", key, {"number": order.order_number, "date": order.order_date, "status": order.status,
                               "net_total": order.net_total, "customer_reference": order.customer_reference})
    g.edge("BusinessPartner", order.customer.number, "PLACED", "SalesOrder", key)
    g.clear_edges("SalesOrder", key, "CONTAINS")
    for item in order.items.select_related("material"):
        g.edge("SalesOrder", key, "CONTAINS", "Material", item.material.number,
               {"quantity": item.quantity, "net_price": item.net_price}, merge_on={"item_number": item.item_number})


def delivery(g, d):
    """``Delivery`` node: the order is DELIVERED_BY it; SHIPPED_FROM a plant; SHIPS each material."""
    key = _doc_key(d, "delivery_number")
    g.node("Delivery", key, {"number": d.delivery_number, "date": d.delivery_date, "status": d.status})
    g.edge("SalesOrder", _doc_key(d.sales_order, "order_number"), "DELIVERED_BY", "Delivery", key)
    g.edge("Delivery", key, "SHIPPED_FROM", "Plant", d.plant.code)
    g.clear_edges("Delivery", key, "SHIPS")
    for item in d.items.select_related("order_item__material"):
        g.edge("Delivery", key, "SHIPS", "Material", item.order_item.material.number, {"quantity": item.quantity},
               merge_on={"item_number": item.order_item.item_number})


def invoice(g, inv):
    """``Invoice`` node: the delivery is BILLED_BY it; BILLED_TO the customer."""
    key = _doc_key(inv, "invoice_number")
    g.node("Invoice", key, {"number": inv.invoice_number, "date": inv.invoice_date, "due_date": inv.due_date,
                            "net_amount": inv.net_amount, "tax_amount": inv.tax_amount,
                            "gross_amount": inv.gross_amount, "status": inv.status})
    g.edge("Delivery", _doc_key(inv.delivery, "delivery_number"), "BILLED_BY", "Invoice", key)
    g.edge("Invoice", key, "BILLED_TO", "BusinessPartner", inv.customer.number)


# --- procure to pay --------------------------------------------------------------------------

def purchase_order(g, order):
    """``PurchaseOrder`` node: ORDERED_FROM the supplier, CONTAINS each material; the supplier
    SUPPLIES each material of a non-cancelled order.
    """
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
    """``GoodsReceipt`` node: the purchase order is RECEIVED_BY it; RECEIVED_AT a plant."""
    key = _doc_key(receipt, "receipt_number")
    g.node("GoodsReceipt", key, {"number": receipt.receipt_number, "date": receipt.receipt_date,
                                 "status": receipt.status, "delivery_note": receipt.delivery_note})
    g.edge("PurchaseOrder", _doc_key(receipt.purchase_order, "order_number"), "RECEIVED_BY", "GoodsReceipt", key)
    g.edge("GoodsReceipt", key, "RECEIVED_AT", "Plant", receipt.plant.code)


def supplier_invoice(g, inv):
    """``SupplierInvoice`` node: the purchase order is BILLED_BY it; BILLED_BY_SUPPLIER the supplier."""
    key = _doc_key(inv, "invoice_number")
    g.node("SupplierInvoice", key, {"number": inv.invoice_number, "supplier_reference": inv.supplier_reference,
                                    "date": inv.invoice_date, "due_date": inv.due_date,
                                    "gross_amount": inv.gross_amount, "status": inv.status})
    g.edge("PurchaseOrder", _doc_key(inv.purchase_order, "order_number"), "BILLED_BY", "SupplierInvoice", key)
    g.edge("SupplierInvoice", key, "BILLED_BY_SUPPLIER", "BusinessPartner", inv.supplier.number)


# --- payments (customer DZ and supplier KZ journal entries) ----------------------------------

PAYMENT_TYPES = {"DZ": "INCOMING", "KZ": "OUTGOING"}


def payment_key(entry):
    """Graph key of a payment: company/fiscal year/document number (payment numbers restart yearly)."""
    return f"{entry.company_code.code}/{entry.fiscal_year}/{entry.document_number}"


def journal_entry(g, entry):
    """``Payment`` node for DZ / KZ entries (other journal entries are not projected): PAID by or
    PAID_TO the partner, and PAID_BY edges from the invoices it cleared. A reversal updates the
    status of the payment it reverses.
    """
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

# --- CRM (Wave 7a) -----------------------------------------------------------------------------

def campaign(g, c):
    """``Campaign`` node: a marketing effort and its budget."""
    g.node("Campaign", _company_child_key(c), {"code": c.code, "name": c.name, "channel": c.channel,
                                               "start_date": c.start_date, "end_date": c.end_date,
                                               "budget": c.budget})


def opportunity(g, o):
    """``Opportunity`` node: the customer HAS_OPPORTUNITY it; FROM_CAMPAIGN when a campaign brought it."""
    key = _doc_key(o, "opportunity_number")
    g.node("Opportunity", key, {"number": o.opportunity_number, "title": o.title, "stage": o.stage,
                                "probability": o.probability, "expected_value": o.expected_value,
                                "expected_close_date": o.expected_close_date, "closed_on": o.closed_on})
    g.edge("BusinessPartner", o.customer.number, "HAS_OPPORTUNITY", "Opportunity", key)
    g.clear_edges("Opportunity", key, "FROM_CAMPAIGN")
    if o.campaign_id:
        g.edge("Opportunity", key, "FROM_CAMPAIGN", "Campaign", _company_child_key(o.campaign))


def quotation(g, q):
    """``Quotation`` node: QUOTED_TO the customer, CONTAINS its materials, ORDERED_AS the sales order it
    became; the opportunity it belongs to is QUOTED_IN it.
    """
    key = _doc_key(q, "quotation_number")
    g.node("Quotation", key, {"number": q.quotation_number, "date": q.quotation_date, "valid_until": q.valid_until,
                              "status": q.status, "net_total": q.net_total})
    g.edge("Quotation", key, "QUOTED_TO", "BusinessPartner", q.customer.number)
    if q.opportunity_id:
        g.edge("Opportunity", _doc_key(q.opportunity, "opportunity_number"), "QUOTED_IN", "Quotation", key)
    g.clear_edges("Quotation", key, "CONTAINS")
    for item in q.items.select_related("material"):
        g.edge("Quotation", key, "CONTAINS", "Material", item.material.number,
               {"quantity": item.quantity, "net_price": item.net_price}, merge_on={"item_number": item.item_number})
    g.clear_edges("Quotation", key, "ORDERED_AS")
    if q.sales_order_id:
        g.edge("Quotation", key, "ORDERED_AS", "SalesOrder", _doc_key(q.sales_order, "order_number"))


def service_ticket(g, t):
    """``ServiceTicket`` node: the customer RAISED it; ABOUT a sales order or a material when given.
    The description and resolution (free text) are not projected.
    """
    key = _doc_key(t, "ticket_number")
    g.node("ServiceTicket", key, {"number": t.ticket_number, "subject": t.subject, "priority": t.priority,
                                  "status": t.status, "date": t.opened_at.date(), "opened_at": t.opened_at,
                                  "due_at": t.due_at, "resolved_at": t.resolved_at})
    g.edge("BusinessPartner", t.customer.number, "RAISED", "ServiceTicket", key)
    g.clear_edges("ServiceTicket", key, "ABOUT")
    if t.sales_order_id:
        g.edge("ServiceTicket", key, "ABOUT", "SalesOrder", _doc_key(t.sales_order, "order_number"))
    if t.material_id:
        g.edge("ServiceTicket", key, "ABOUT", "Material", t.material.number)


# --- SRM (Wave 7b) -----------------------------------------------------------------------------

def rfq(g, r):
    """``RFQ`` node: REQUESTS its materials, INVITED each supplier (with the quote's status and
    total), AWARDED_AS the purchase order it became."""
    from apps.srm.services import quote_total

    key = _doc_key(r, "rfq_number")
    g.node("RFQ", key, {"number": r.rfq_number, "title": r.title, "deadline": r.deadline, "status": r.status})
    for rel in ("REQUESTS", "INVITED", "AWARDED_AS"):
        g.clear_edges("RFQ", key, rel)
    for item in r.items.select_related("material"):
        g.edge("RFQ", key, "REQUESTS", "Material", item.material.number, {"quantity": item.quantity},
               merge_on={"item_number": item.item_number})
    for quote in r.quotes.select_related("supplier"):
        g.edge("RFQ", key, "INVITED", "BusinessPartner", quote.supplier.number,
               {"status": quote.status, "delivery_days": quote.delivery_days,
                "total": quote_total(quote) if quote.status != "INVITED" else None})
    if r.purchase_order_id:
        g.edge("RFQ", key, "AWARDED_AS", "PurchaseOrder", _doc_key(r.purchase_order, "order_number"))


def contract(g, c):
    """``Contract`` node: WITH its supplier, COVERS its materials at the agreed price, RELEASED_AS
    each purchase order released against it."""
    key = _doc_key(c, "contract_number")
    g.node("Contract", key, {"number": c.contract_number, "status": c.status, "valid_from": c.valid_from,
                             "valid_to": c.valid_to})
    for rel in ("WITH", "COVERS", "RELEASED_AS"):
        g.clear_edges("Contract", key, rel)
    g.edge("Contract", key, "WITH", "BusinessPartner", c.supplier.number)
    for item in c.items.select_related("material"):
        g.edge("Contract", key, "COVERS", "Material", item.material.number,
               {"net_price": item.net_price, "target_quantity": item.target_quantity})
    for release in c.releases.select_related("purchase_order"):
        g.edge("Contract", key, "RELEASED_AS", "PurchaseOrder", _doc_key(release.purchase_order, "order_number"))


def supplier_evaluation(g, e):
    """The supplier's latest grade and score, as properties of its ``BusinessPartner`` node."""
    from apps.srm.models import SupplierEvaluation

    latest = SupplierEvaluation.objects.filter(supplier=e.supplier).order_by("-period_end").first()
    g.node("BusinessPartner", e.supplier.number, {
        "supplier_grade": latest.grade if latest else None,
        "supplier_score": latest.overall_score if latest else None,
        "supplier_evaluated_to": latest.period_end if latest else None})


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
    "production.productionarea": (production_area, lambda o: ("Area", _plant_child_key(o))),
    "production.productionline": (production_line, lambda o: ("Line", _plant_child_key(o))),
    "production.workcenter": (work_center, lambda o: ("Cell", _plant_child_key(o))),
    "production.cellstate": (cell_state, None),  # onto its Cell node
    "production.billofmaterials": (bill_of_materials, lambda o: ("BOM", o.key)),
    "production.routing": (routing, lambda o: ("Routing", o.key)),
    "production.productionorder": (production_order, lambda o: ("ProductionOrder", _doc_key(o, "order_number"))),
    "crm.campaign": (campaign, lambda o: ("Campaign", _company_child_key(o))),
    "crm.opportunity": (opportunity, lambda o: ("Opportunity", _doc_key(o, "opportunity_number"))),
    "crm.quotation": (quotation, lambda o: ("Quotation", _doc_key(o, "quotation_number"))),
    "crm.serviceticket": (service_ticket, lambda o: ("ServiceTicket", _doc_key(o, "ticket_number"))),
    "srm.requestforquotation": (rfq, lambda o: ("RFQ", _doc_key(o, "rfq_number"))),
    "srm.supplierquote": (lambda g, q: rfq(g, q.rfq), None),  # onto its RFQ
    "srm.contract": (contract, lambda o: ("Contract", _doc_key(o, "contract_number"))),
    "srm.contractrelease": (lambda g, r: contract(g, r.contract), None),  # onto its contract
    "srm.supplierevaluation": (supplier_evaluation, None),  # onto the supplier's node
}


def model_key(instance) -> str:
    """``app_label.model`` of an instance, the key into ``PROJECTORS``."""
    return f"{instance._meta.app_label}.{instance._meta.model_name}"


def project_instance(g, instance):
    """Project one record that is already loaded (used by the full rebuild)."""
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
        if aggregate_type in ("masterdata.customerrole", "masterdata.supplierrole", "controlling.plantprofitcenter",
                              "production.cellstate", "srm.supplierquote", "srm.contractrelease"):
            continue  # projected through their business partner, plant or work centre
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
