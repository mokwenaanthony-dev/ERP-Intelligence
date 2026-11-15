# 0015. MRP nets dated requirements into planned orders; people convert them through the module services

- **Status:** Accepted, 2026-11-14 (Wave 8b)

## Context

With demand forecasts stored (Wave 8a, ADR 0014), the ERP can answer the planner's question: what
must be made or bought, how much, and by when, so that customers are served from stock? This is
material requirements planning (MRP, SAP MD01/MD04). It has to combine five sources the ERP already
holds: forecasts, open sales orders, stock, open purchase and production orders, and BOMs.

## Decision

- **Planning parameters per material and plant** (`MaterialPlanning`): make or buy, lot-size rule
  (lot for lot, fixed lots, minimum lot), safety stock, lead time in days, preferred supplier.
  Optional: without them, a material with an active BOM is made, others are bought, lot for lot.
- **Snapshot, then a pure planner.** MRP first reads everything it depends on into plain data (the
  snapshot), then plans with a pure function (`mrp.plan`) that reads no database and is tested on
  hand-built snapshots. The snapshot's SHA-256 is the run's **fingerprint**: MRP on unchanged data
  (same date) returns the stored run, so the nightly task and repeated requests are idempotent
  (ADR 0010).
- **Dated requirements, monthly reading.** Requirements and receipts keep their dates; the planner
  processes them in date order (receipts first), and a planned order is due on the day the stock
  would fall below the safety stock, started lead time earlier. A first version used monthly
  buckets and made every component due at the start of the month, which flagged orders as late that
  were not; dated netting fixed that. The requirements list is still summed per month for reading.
- **Demand:** sales orders on their dates, plus the part of each month's forecast that the month's
  orders do not cover (orders consume the forecast). Deliveries already posted this month are taken
  off its forecast. Overdue items count on the planning date.
- **Low-level codes**: materials are planned from finished products down, so a component sees all
  the dependent demand of its parents' planned orders (on their start dates) and of open production
  orders (components not yet issued).
- **Regenerative runs, never edited.** Each run is numbered (MR) and kept; a new run supersedes the
  open planned orders of earlier runs. Messages tell the planner what needs a decision: orders that
  should already have started, MAKE materials without a BOM.
- **Planned orders are proposals; people convert them** through the services that own the real
  documents: a MAKE order becomes a production order (`apps.production.orders.create_order`, with
  its BOM, routing and cost estimate); a BUY order becomes a release against an active SRM contract
  that still covers the quantity (ADR 0013), otherwise a purchase order to the given, preferred or
  last supplier at the given or last price. Converting twice returns the same document. Converting
  follows the role that owns the result: production planners convert MAKE, purchasing officers BUY.

## Alternatives considered

- **Planned orders written straight into production and purchasing:** faster, but no human check
  and no way to see a plan before it commits the company.
- **Monthly buckets throughout:** simpler, but too coarse for lead times of days (see above).
- **Net change MRP** (only re-plan what changed): faster for large data, but harder to make
  idempotent and to explain; regenerative is fast enough here.
- **Capacity-constrained planning (APS):** the real next step for a factory, but needs routings with
  calendars and machine capacities; MRP assumes infinite capacity, as SAP's does.

## Consequences

- A sales order for a finished product now drives frames, steel and fasteners, with dates, and the
  plan can be turned into real orders in two clicks per line.
- Forecast quality matters: an inflated forecast buys stock; MAE and MASE are stored with forecasts
  so the planner can judge them.
- Not yet: capacity, BOM versions changing inside the horizon, scrap rates, working-day calendars,
  planned orders on the knowledge graph (Wave 8c).
