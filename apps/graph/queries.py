"""Questions the knowledge graph answers, as Cypher traversals.

Each function takes the set of labels the caller may see (``access.allowed_labels``) and never
returns nodes outside it, so the same role rules protect the API, the graph and any AI agent that
reads from it.
"""
from .projection import DOCUMENT_LABELS
from .store import _name, get_store

FLOW = "DELIVERED_BY|BILLED_BY|PAID_BY|RECEIVED_BY"
PREFIX_LABELS = {"SO": "SalesOrder", "DL": "Delivery", "IN": "Invoice", "PO": "PurchaseOrder",
                 "GR": "GoodsReceipt", "IV": "SupplierInvoice", "DZ": "Payment", "KZ": "Payment"}
PARTNER_LINKS = "PLACED|BILLED_TO|PAID|PAID_TO|ORDERED_FROM|BILLED_BY_SUPPLIER|DELIVERED_BY|BILLED_BY|RECEIVED_BY"


def _money(value):
    return round(float(value or 0), 2)


def _type(node, allowed):
    return next((label for label in node["labels"] if label in allowed and label != "Customer"
                 and label != "Supplier"), node["labels"][0])


# --- Customer 360 ------------------------------------------------------------------------------

def customer_360(number: str, allowed: set) -> dict | None:
    """Everything connected to one business partner, filtered to the labels the caller may see."""
    read = get_store().read
    rows = read("MATCH (bp:BusinessPartner {key: $key}) "
                "OPTIONAL MATCH (bp)-[r:CUSTOMER_OF|SUPPLIER_OF]->(c:CompanyCode) "
                "RETURN bp AS partner, collect({role: type(r), company: c.key, "
                "payment_terms_days: r.payment_terms_days}) AS roles", {"key": number})
    if not rows:
        return None
    partner = rows[0]["partner"]
    result = {
        "business_partner": {k: partner.get(k) for k in ("key", "name", "category", "city", "country",
                                                         "vat_number", "is_blocked")},
        "roles": [r for r in rows[0]["roles"] if r["role"]],
    }
    result["business_partner"]["number"] = result["business_partner"].pop("key")

    orders = read("MATCH (:BusinessPartner {key: $key})-[:PLACED]->(so:SalesOrder) "
                  "RETURN so.status AS status, count(so) AS orders, sum(so.net_total) AS net_total "
                  "ORDER BY status", {"key": number})
    result["sales_orders"] = [{**row, "net_total": _money(row["net_total"])} for row in orders]

    [billing] = read(
        "OPTIONAL MATCH (inv:Invoice {status: 'POSTED'})-[:BILLED_TO]->(:BusinessPartner {key: $key}) "
        "OPTIONAL MATCH (inv)-[p:PAID_BY]->(pay:Payment {status: 'POSTED'}) "
        "WITH inv, coalesce(sum(p.amount), 0) AS paid WHERE inv IS NOT NULL "
        "WITH inv, paid, inv.gross_amount - paid AS unpaid "
        "RETURN count(inv) AS invoices, sum(inv.gross_amount) AS billed, sum(paid) AS paid, "
        "sum(CASE WHEN unpaid > 0.005 THEN unpaid ELSE 0 END) AS open_amount, "
        "sum(CASE WHEN unpaid > 0.005 AND inv.due_date < date() THEN unpaid ELSE 0 END) AS overdue_amount",
        {"key": number})
    amounts = ("billed", "paid", "open_amount", "overdue_amount")
    result["billing"] = {"invoices": billing["invoices"], **{k: _money(billing[k]) for k in amounts}}

    result["top_materials"] = [
        {**row, "quantity": round(float(row["quantity"]), 3), "revenue": _money(row["revenue"])}
        for row in read("MATCH (:BusinessPartner {key: $key})-[:PLACED]->(so:SalesOrder)-[c:CONTAINS]->(m:Material) "
                        "WHERE so.status <> 'CANCELLED' "
                        "RETURN m.key AS material, m.description AS description, sum(c.quantity) AS quantity, "
                        "sum(c.quantity * c.net_price) AS revenue ORDER BY revenue DESC LIMIT 5", {"key": number})
    ]
    if "PurchaseOrder" in allowed:
        [supplier] = read("MATCH (bp:BusinessPartner {key: $key}) "
                          "OPTIONAL MATCH (bp)-[:SUPPLIES]->(m:Material) "
                          "WITH bp, collect(DISTINCT m.key) AS materials "
                          "OPTIONAL MATCH (po:PurchaseOrder)-[:ORDERED_FROM]->(bp) WHERE po.status <> 'CANCELLED' "
                          "RETURN materials, count(po) AS purchase_orders, sum(po.net_total) AS purchased",
                          {"key": number})
        result["as_supplier"] = {"materials": sorted(supplier["materials"]),
                                 "purchase_orders": supplier["purchase_orders"],
                                 "purchased": _money(supplier["purchased"])}
    result["recent_documents"] = [
        {"type": _type(row["document"], allowed), "key": row["document"]["key"],
         "date": row["document"].get("date"), "status": row["document"].get("status")}
        for row in read(f"MATCH (:BusinessPartner {{key: $key}})-[:{PARTNER_LINKS}*1..2]-(d) "
                        "WHERE d.date IS NOT NULL AND any(l IN labels(d) WHERE l IN $labels) "
                        "RETURN DISTINCT d AS document ORDER BY document.date DESC, document.key DESC LIMIT 10",
                        {"key": number, "labels": sorted(allowed & set(DOCUMENT_LABELS))})
    ]
    return result


# --- Document flow -----------------------------------------------------------------------------

def document_flow(company_code: str, number: str, allowed: set) -> dict | None:
    """Everything before and after one document in its process chain, like SAP's document flow."""
    label = PREFIX_LABELS.get(number[:2].upper())
    if label is None:
        raise ValueError(f"Unknown document type in {number!r}: use SO, DL, IN, PO, GR, IV, DZ or KZ numbers.")
    if label not in allowed:
        raise PermissionError(label)
    if label == "Payment":
        start = "MATCH (start:Payment {number: $number}) WHERE start.key STARTS WITH $company + '/'"
    else:
        start = f"MATCH (start:`{_name(label)}` {{key: $company + '/' + $number}})"
    rows = get_store().read(
        f"{start} "
        f"OPTIONAL MATCH (start)-[:{FLOW}*1..4]->(down) WITH start, collect(DISTINCT down) AS downs "
        f"OPTIONAL MATCH (up)-[:{FLOW}*1..4]->(start) WITH start, downs, collect(DISTINCT up) AS ups "
        "WITH [start] + downs + ups AS docs UNWIND docs AS a "
        f"OPTIONAL MATCH (a)-[r:{FLOW}]->(b) WHERE b IN docs "
        "RETURN a AS document, collect(CASE WHEN b IS NULL THEN null ELSE {type: type(r), to: b.key} END) AS links",
        {"company": company_code, "number": number.upper()})
    if not rows:
        return None
    documents = []
    for row in rows:
        node = row["document"]
        doc_type = _type(node, allowed)
        if doc_type not in allowed:
            continue  # e.g. a sales rep sees the order chain but not the supplier side
        documents.append({"type": doc_type, **{k: v for k, v in node.items() if k != "labels"},
                          "links": sorted(row["links"], key=lambda link: link["to"])})
    order = {name: index for index, name in enumerate(DOCUMENT_LABELS)}
    documents.sort(key=lambda d: (order.get(d["type"], 99), d.get("date") or "", d["key"]))
    return {"start": f"{company_code}/{number.upper()}", "documents": documents}


# --- Where used (multi-level BOM) -------------------------------------------------------------

MAX_BOM_LEVELS = 10
OPEN_ORDER_STATUSES = ["OPEN", "CONFIRMED"]


def where_used(material: str, allowed: set) -> dict | None:
    """Every product that consumes ``material`` through active BOMs, at any depth; the open sales orders
    for those products (if the caller may see sales); and the open production orders that need the
    material or one of those products (if the caller may see production orders).

    This is the supply-chain risk question "if this component is short, what can we not make, and
    whose orders are affected?". The traversal is one level per query, so each step stays a simple,
    indexed pattern and the depth is bounded.
    """
    read = get_store().read
    found = read("MATCH (m:Material {key: $key}) RETURN m.key AS key, m.description AS description",
                 {"key": material})
    if not found:
        return None
    used_in, frontier, seen = [], [material], {material}
    for level in range(1, MAX_BOM_LEVELS + 1):
        rows = read(
            "MATCH (b:BOM {status: 'ACTIVE'})-[r:CONSUMES]->(c:Material) WHERE c.key IN $keys "
            "MATCH (b)-[:FOR]->(p:Material) "
            "RETURN c.key AS component, p.key AS product, p.description AS description, "
            "p.material_type AS material_type, b.plant AS plant, b.key AS bom, r.quantity AS quantity "
            "ORDER BY product, plant",
            {"keys": frontier})
        frontier = []
        for row in rows:
            used_in.append({"level": level, **row})
            if row["product"] not in seen:
                seen.add(row["product"])
                frontier.append(row["product"])
        if not frontier:
            break
    result = {"material": found[0]["key"], "description": found[0]["description"], "used_in": used_in}
    if "SalesOrder" in allowed and "BusinessPartner" in allowed:
        products = sorted({row["product"] for row in used_in} | {material})
        orders = read(
            "MATCH (c:BusinessPartner)-[:PLACED]->(so:SalesOrder)-[ci:CONTAINS]->(m:Material) "
            "WHERE m.key IN $keys AND so.status IN $statuses "
            "RETURN so.key AS sales_order, so.status AS status, so.date AS date, c.name AS customer, "
            "m.key AS product, ci.quantity AS quantity ORDER BY sales_order, product",
            {"keys": products, "statuses": OPEN_ORDER_STATUSES})
        result["open_sales_orders"] = [dict(row) for row in orders]
    if "ProductionOrder" in allowed:
        # Orders already planned or running that need the material, or a product made from it.
        components = sorted({row["component"] for row in used_in} | {material})
        planned = read(
            "MATCH (po:ProductionOrder)-[c:CONSUMES]->(m:Material) "
            "WHERE m.key IN $keys AND po.status IN ['CREATED', 'RELEASED'] "
            "MATCH (po)-[:PRODUCES]->(p:Material) "
            "RETURN po.key AS production_order, po.status AS status, po.start_date AS start_date, "
            "p.key AS product, m.key AS component, c.quantity AS quantity ORDER BY production_order, component",
            {"keys": components})
        result["open_production_orders"] = [dict(row) for row in planned]
    return result


# --- Shop floor (Wave 6d) --------------------------------------------------------------------------

ATTENTION_STATES = ["DOWN", "OFFLINE"]


def shop_floor(plant: str, allowed: set) -> dict:
    """Every cell of a plant with its live state; for cells that are down or offline, the released
    production orders with operations there and the open sales orders for what those orders make.

    The question is "which cells need attention, and which orders do they hold up?": a traversal
    from the cell through the orders that need it to the customers waiting, filtered to what the
    caller's role may see.
    """
    read = get_store().read
    cells = [dict(row) for row in read(
        "MATCH (c:Cell)-[:PART_OF]->(:Line)-[:PART_OF]->(:Area)-[:PART_OF]->(:Plant {key: $plant}) "
        "RETURN c.key AS cell, c.code AS code, c.name AS name, c.uns_path AS uns_path, c.state AS state, "
        "c.state_since AS since, c.state_order AS running_order, c.state_operation AS operation_number "
        "ORDER BY cell", {"plant": plant})]
    summary = {}
    for cell in cells:
        summary[cell["state"] or "UNKNOWN"] = summary.get(cell["state"] or "UNKNOWN", 0) + 1
    attention = [cell for cell in cells if cell["state"] in ATTENTION_STATES]
    for cell in attention:
        cell["held_up"] = []
    if attention and "ProductionOrder" in allowed:
        by_cell = {cell["cell"]: cell for cell in attention}
        rows = read(
            "MATCH (po:ProductionOrder {status: 'RELEASED'})-[op:HAS_OPERATION]->(c:Cell) WHERE c.key IN $cells "
            "MATCH (po)-[:PRODUCES]->(m:Material) "
            "RETURN c.key AS cell, po.key AS production_order, op.operation_number AS operation_number, "
            "po.start_date AS start_date, po.finish_date AS finish_date, m.key AS product "
            "ORDER BY cell, start_date, production_order",
            {"cells": sorted(by_cell)})
        for row in rows:
            by_cell[row["cell"]]["held_up"].append({k: row[k] for k in row if k != "cell"})
        if "SalesOrder" in allowed and "BusinessPartner" in allowed:
            products = sorted({o["product"] for cell in attention for o in cell["held_up"]})
            waiting = read(
                "MATCH (cu:BusinessPartner)-[:PLACED]->(so:SalesOrder)-[ci:CONTAINS]->(m:Material) "
                "WHERE m.key IN $keys AND so.status IN $statuses "
                "RETURN m.key AS product, so.key AS sales_order, cu.name AS customer, ci.quantity AS quantity "
                "ORDER BY sales_order", {"keys": products, "statuses": OPEN_ORDER_STATUSES})
            for cell in attention:
                for order in cell["held_up"]:
                    order["open_sales_orders"] = [{k: w[k] for k in w if k != "product"}
                                                  for w in waiting if w["product"] == order["product"]]
    return {"plant": plant, "states": summary, "cells": cells, "needs_attention": attention}


# --- Supplier dependency -----------------------------------------------------------------------

def supplier_dependency(company_code: str, allowed: set) -> list:
    """Per material: how many suppliers it has and which customers' sales depend on it."""
    rows = get_store().read(
        "MATCH (s:BusinessPartner)-[:SUPPLIER_OF]->(:CompanyCode {key: $company}) "
        "MATCH (s)-[:SUPPLIES]->(m:Material) "
        "WITH m, collect(DISTINCT s.name) AS suppliers "
        "OPTIONAL MATCH (c:BusinessPartner)-[:PLACED]->(so:SalesOrder)-[ci:CONTAINS]->(m) "
        "WHERE so.status <> 'CANCELLED' AND so.key STARTS WITH $company + '/' "
        "WITH m, suppliers, collect(DISTINCT c.name) AS customers, sum(ci.quantity * ci.net_price) AS sales "
        "RETURN m.key AS material, m.description AS description, suppliers, size(suppliers) AS supplier_count, "
        "customers, sales ORDER BY supplier_count, sales DESC",
        {"company": company_code})
    show_customers = "SalesOrder" in allowed
    return [{
        "material": row["material"], "description": row["description"], "suppliers": sorted(row["suppliers"]),
        "supplier_count": row["supplier_count"], "single_source": row["supplier_count"] == 1,
        **({"customers": sorted(row["customers"]), "sales_value": _money(row["sales"])} if show_customers else {}),
    } for row in rows]


# --- KG-RAG retrieval -----------------------------------------------------------------------------

HIDDEN_PROPERTIES = {"key", "labels"}


def _describe(node, allowed):
    return f"{_type(node, allowed)} {node['key']}"


def context(label: str, key: str, allowed: set, depth: int = 2, limit: int = 60) -> dict | None:
    """The neighbourhood of one node as grounded facts for an AI agent (knowledge-graph retrieval, KG-RAG).

    Every node on every path must be a label the caller may see, so an agent acting for a sales rep
    can never reach an employee record, however the graph is connected.
    """
    if label not in allowed:
        raise PermissionError(label)
    depth = max(1, min(int(depth), 3))
    rows = get_store().read(
        f"MATCH (start:`{_name(label)}` {{key: $key}}) "
        f"OPTIONAL MATCH path = (start)-[*1..{depth}]-(n) "
        "WHERE all(x IN nodes(path) WHERE any(l IN labels(x) WHERE l IN $allowed)) "
        "WITH start, collect(path)[..2000] AS paths "
        "WITH start, reduce(acc = [], p IN paths | acc + relationships(p)) AS rels "
        "UNWIND (CASE WHEN size(rels) = 0 THEN [null] ELSE rels END) AS r "
        "WITH DISTINCT start, r LIMIT $limit "
        "RETURN start, startNode(r) AS a, type(r) AS type, endNode(r) AS b, properties(r) AS props",
        {"key": key, "allowed": sorted(allowed), "limit": limit})
    if not rows:
        return None
    nodes, facts = {}, []
    for row in rows:
        for node in (row["start"], row["a"], row["b"]):
            if node:
                nodes[node["key"]] = {"type": _type(node, allowed),
                                      **{k: v for k, v in node.items() if k not in HIDDEN_PROPERTIES}}
        if row["type"]:
            detail = ", ".join(f"{k}: {v}" for k, v in sorted((row["props"] or {}).items()))
            facts.append({"subject": _describe(row["a"], allowed), "predicate": row["type"],
                          "object": _describe(row["b"], allowed), "properties": row["props"] or {},
                          "text": f"{_describe(row['a'], allowed)} {row['type']} {_describe(row['b'], allowed)}"
                                  + (f" ({detail})" if detail else "")})
    return {"start": {"label": label, "key": key}, "depth": depth, "nodes": nodes, "facts": facts}
