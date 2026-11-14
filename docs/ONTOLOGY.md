# Knowledge graph ontology

**Version 1.4** (Wave 7a: CRM). 1.3 added live cell state (Wave 6d); 1.2 production orders (Wave 5b);
1.1 the equipment hierarchy, BOMs and routings (Wave 5a); 1.0 was Wave 4b.

This document defines what every node type and relationship in the knowledge graph means, how it is
keyed and which ERP data it comes from. It is the contract for anyone who queries the graph: people,
reports, the KG-RAG endpoint and AI agents. The code that implements it is
`apps/graph/projection.py`; the reasons behind it are in [ADR 0004](decisions/0004-neo4j-property-graph-and-kg-rag.md)
and [ADR 0006](decisions/0006-isa-95-equipment-hierarchy-as-shared-vocabulary.md).

## Rules that apply to everything

- **The graph is a read model.** It is projected from the ERP and never written to directly. If the
  graph and the ERP disagree, the ERP is right; `graph_rebuild` repairs the graph.
- **Every node has a `key`**, its business key, unique per label (Neo4j enforces this with a
  uniqueness constraint per label). Keys are the identifiers people and other systems already use.
- **Key patterns:** `<number>` for globally numbered master data (partners, materials); `<company>/<code>`
  for company-level objects; `<plant>/<code>` for plant-level objects; `<company>/<document number>`
  for documents; `<plant>/<material>/<valid from>` for versioned BOMs and routings.
- **Money and quantities are floats** (Neo4j has no decimal type). The ERP holds the exact amounts.
- **Never in the graph:** salaries, tax references, bank details, passwords or any pay data.
- **Access:** each role sees only some labels (`apps/graph/access.py`); every query filters by them.
- **Changing this ontology:** adding a property or relationship is a minor version (1.1 → 1.2). Renaming
  or removing one, or changing a key, is a major version (→ 2.0) and needs `graph_rebuild --clear`.
  Update this document, `ONTOLOGY_VERSION` in `projection.py` and the tests together.

## Organisation and controlling

| Label | Key | Properties | Source |
|---|---|---|---|
| `CompanyCode` | `1000` | name, currency | `organization.CompanyCode` |
| `Plant` | `1000` | name | `organization.Plant` |
| `ProfitCenter` | `1000/PC1000` | code, name, is_active | `controlling.ProfitCenter` |
| `CostCenter` | `1000/4000` | code, name, category, is_active | `controlling.CostCenter` |

| Relationship | Meaning |
|---|---|
| `(Plant)-[:BELONGS_TO]->(CompanyCode)` | The plant is a site of the company |
| `(Plant)-[:ASSIGNED_TO]->(ProfitCenter)` | Default profit centre of the plant's postings |
| `(ProfitCenter)-[:BELONGS_TO]->(CompanyCode)` | |
| `(CostCenter)-[:BELONGS_TO]->(ProfitCenter)` | The cost centre's results roll up to this profit centre |

## Equipment hierarchy (ISA-95) — new in 1.1

The graph uses ISA-95 names. The ERP's SAP-style "work centre" is an ISA-95 **work unit** and is
labelled `Cell` here ([ADR 0006](decisions/0006-isa-95-equipment-hierarchy-as-shared-vocabulary.md)).

| Label | ISA-95 level | Key | Properties | Source |
|---|---|---|---|---|
| `Area` | Area | `1000/ASSY` | code, name, isa95_level, uns_path, is_active | `production.ProductionArea` |
| `Line` | Work centre, type production line | `1000/L1` | code, name, isa95_level, isa95_type, uns_path, is_active | `production.ProductionLine` |
| `Cell` | Work unit, normally a work cell | `1000/PRESS3` | code, name, isa95_level, uns_path, capacity_hours_per_day, activity_types, is_active; live state (1.3): state, state_since, state_order, state_operation, state_reported_at | `production.WorkCenter`, `production.CellState` |

| Relationship | Meaning |
|---|---|
| `(Area)-[:PART_OF]->(Plant)` | |
| `(Line)-[:PART_OF]->(Area)` | |
| `(Cell)-[:PART_OF]->(Line)` | |
| `(Cell)-[:CHARGED_TO]->(CostCenter)` | Activity at this cell (machine and labour hours) is costed through this cost centre |
| `(Cell)-[:RUNNING]->(ProductionOrder)` | operation_number, since: the order the cell is working on now (new in 1.3); only while RUNNING, and only to an order the ERP has |

`uns_path` is the cell's Unified Namespace topic path, e.g. `1000/1000/assy/l1/press3`
([ADR 0007](decisions/0007-unified-namespace-conventions.md)): live state for the node is published
under that path. Since 1.3 the node also **carries** that state: the UNS listener keeps each cell's
latest retained `state/status` in `production.CellState`, and the outbox projects it here
([ADR 0011](decisions/0011-live-cell-state-in-the-erp-and-the-graph.md)). `state` is one of
`RUNNING`, `IDLE`, `SETUP`, `DOWN`, `OFFLINE`; a cell that has never reported has no `state` at all
(unknown, not guessed). Like everything else, it is rebuilt from ERP tables by `graph_rebuild`.

## Materials, BOMs and routings

| Label | Key | Properties | Source |
|---|---|---|---|
| `Material` | `FG-4001` | description, material_type (HAWA, FERT, HALB, ROH, DIEN), is_active | `masterdata.Material` |
| `BOM` | `1000/FG-4001/2026-03-01` | material, plant, valid_from, base_quantity, status | `production.BillOfMaterials` (new in 1.1) |
| `Routing` | `1000/FG-4001/2026-03-01` | material, plant, valid_from, base_quantity, status | `production.Routing` (new in 1.1) |

| Relationship | Properties | Meaning |
|---|---|---|
| `(BOM)-[:FOR]->(Material)` | | The product this BOM makes |
| `(BOM)-[:CONSUMES]->(Material)` | item_number, quantity | A component, per `base_quantity` of the product |
| `(Routing)-[:FOR]->(Material)` | | The product this routing makes |
| `(Routing)-[:HAS_OPERATION]->(Cell)` | operation_number, description | An operation performed at this cell |

Only `status: 'ACTIVE'` BOMs count for explosions and where-used. Several versions of one product can
exist; the ERP picks the one valid on a date.

## Business partners and people

| Label | Key | Properties | Source |
|---|---|---|---|
| `BusinessPartner` (+ `Customer`, `Supplier`) | `1000001` | name, category, city, country, vat_number, is_blocked | `masterdata.BusinessPartner` and its roles |
| `OrgUnit` | `1000/SALES` | code, name | `hr.OrgUnit` |
| `Position` | `1000/SALES/SREP` | code, title | `hr.Position` |
| `Employee` | `1000/PN00000002` | personnel_number, name, hire_date, termination_date (**no pay data**) | `hr.Employee` |

| Relationship | Properties | Meaning |
|---|---|---|
| `(BusinessPartner)-[:CUSTOMER_OF]->(CompanyCode)` | payment_terms_days, credit_limit | Customer role in that company |
| `(BusinessPartner)-[:SUPPLIER_OF]->(CompanyCode)` | payment_terms_days | Supplier role in that company |
| `(BusinessPartner)-[:SUPPLIES]->(Material)` | | Has been ordered from this supplier (non-cancelled PO) |
| `(OrgUnit)-[:BELONGS_TO]->(CompanyCode)`, `(OrgUnit)-[:PART_OF]->(OrgUnit)`, `(OrgUnit)-[:CHARGED_TO]->(CostCenter)` | | Organisation tree and its cost centre |
| `(Position)-[:IN]->(OrgUnit)`, `(Position)-[:CHARGED_TO]->(CostCenter)` | | |
| `(Employee)-[:HOLDS]->(Position)`, `(Employee)-[:EMPLOYED_BY]->(CompanyCode)` | | |

## Documents

Keys are `<company>/<document number>`; payments are `<company>/<fiscal year>/<document number>`
because accounting numbers restart each year.

| Label | Properties | Source |
|---|---|---|
| `SalesOrder` | number, date, status, net_total, customer_reference | `sales.SalesOrder` |
| `Delivery` | number, date, status | `sales.Delivery` |
| `Invoice` | number, date, due_date, net_amount, tax_amount, gross_amount, status | `sales.Invoice` |
| `PurchaseOrder` | number, date, status, net_total | `purchasing.PurchaseOrder` |
| `GoodsReceipt` | number, date, status, delivery_note | `purchasing.GoodsReceipt` |
| `SupplierInvoice` | number, supplier_reference, date, due_date, gross_amount, status | `purchasing.SupplierInvoice` |
| `Payment` | number, date, status, direction (INCOMING/OUTGOING), reference, amount | DZ / KZ `finance.JournalEntry` |
| `ProductionOrder` | number, status, quantity, start_date, finish_date, planned_cost | `production.ProductionOrder` (new in 1.2) |

| Relationship | Properties | Meaning |
|---|---|---|
| `(BusinessPartner)-[:PLACED]->(SalesOrder)` | | |
| `(SalesOrder)-[:CONTAINS]->(Material)`, `(PurchaseOrder)-[:CONTAINS]->(Material)` | item_number, quantity, net_price | Order items |
| `(SalesOrder)-[:DELIVERED_BY]->(Delivery)` | | Document flow |
| `(Delivery)-[:SHIPPED_FROM]->(Plant)`, `(Delivery)-[:SHIPS]->(Material)` | item_number, quantity | |
| `(Delivery)-[:BILLED_BY]->(Invoice)`, `(Invoice)-[:BILLED_TO]->(BusinessPartner)` | | |
| `(PurchaseOrder)-[:ORDERED_FROM]->(BusinessPartner)` | | |
| `(PurchaseOrder)-[:RECEIVED_BY]->(GoodsReceipt)`, `(GoodsReceipt)-[:RECEIVED_AT]->(Plant)` | | |
| `(PurchaseOrder)-[:BILLED_BY]->(SupplierInvoice)`, `(SupplierInvoice)-[:BILLED_BY_SUPPLIER]->(BusinessPartner)` | | |
| `(BusinessPartner)-[:PAID]->(Payment)`, `(Payment)-[:PAID_TO]->(BusinessPartner)` | | Incoming / outgoing |
| `(Invoice\|SupplierInvoice)-[:PAID_BY]->(Payment)` | amount | Open items the payment cleared |
| `(ProductionOrder)-[:PRODUCES]->(Material)` | | The product the order makes (new in 1.2) |
| `(ProductionOrder)-[:MADE_AT]->(Plant)` | | |
| `(ProductionOrder)-[:CONSUMES]->(Material)` | item_number, quantity | A component, quantity for the whole order |
| `(ProductionOrder)-[:HAS_OPERATION]->(Cell)` | operation_number, description | Where each operation runs |

## CRM — new in 1.4

| Label | Key | Properties | Source |
|---|---|---|---|
| `Campaign` | `1000/SPRING-EXPO` | code, name, channel, start_date, end_date, budget | `crm.Campaign` |
| `Opportunity` | `1000/OP00000001` | number, title, stage, probability, expected_value, expected_close_date, closed_on | `crm.Opportunity` |
| `Quotation` | `1000/QT00000001` | number, date, valid_until, status, net_total | `crm.Quotation` |
| `ServiceTicket` | `1000/ST00000001` | number, subject, priority, status, date, opened_at, due_at, resolved_at | `crm.ServiceTicket` |

| Relationship | Properties | Meaning |
|---|---|---|
| `(BusinessPartner)-[:HAS_OPPORTUNITY]->(Opportunity)` | | A potential sale to this customer |
| `(Opportunity)-[:FROM_CAMPAIGN]->(Campaign)` | | The campaign that brought it |
| `(Opportunity)-[:QUOTED_IN]->(Quotation)` | | An offer made for it |
| `(Quotation)-[:QUOTED_TO]->(BusinessPartner)` | | The customer it was offered to |
| `(Quotation)-[:CONTAINS]->(Material)` | item_number, quantity, net_price | What was offered, at what price |
| `(Quotation)-[:ORDERED_AS]->(SalesOrder)` | | The sales order it became when accepted (part of the document flow) |
| `(BusinessPartner)-[:RAISED]->(ServiceTicket)` | | The customer's request or complaint |
| `(ServiceTicket)-[:ABOUT]->(SalesOrder \| Material)` | | What it concerns |

**Not in the graph:** leads (their contact names, e-mail addresses and phone numbers are personal data,
and a lead is not a customer yet), and the free text of tickets (description, resolution), which may
name people ([ADR 0012](decisions/0012-crm-in-the-monolith-on-the-business-partner.md)). Opportunities,
quotations, tickets and campaigns are visible to the roles that see sales.

## Queries built on this ontology

| Endpoint | Question |
|---|---|
| `/api/graph/customer-360/<number>/` | Everything about a business partner; since 1.4 its pipeline and open and overdue tickets |
| `/api/graph/document-flow/` | Quotation → order → delivery → invoice → payment (and the purchasing chain) |
| `/api/graph/supplier-dependency/` | Single-source materials and the customers that depend on them |
| `/api/graph/where-used/<material>/` | Products that use a material at any BOM level, their open sales orders, and open production orders that need it (1.1, orders in 1.2) |
| `/api/graph/shop-floor/?plant=` | Every cell's live state; for cells DOWN or OFFLINE, the released production orders and open sales orders they hold up (1.3) |
| `/api/graph/context/` | KG-RAG: a node's neighbourhood as grounded facts for an AI agent |
