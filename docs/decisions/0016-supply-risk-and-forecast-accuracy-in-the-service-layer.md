# 0016. Revenue at risk and forecast accuracy are computed in the service layer; MRP re-checked every 15 minutes

- **Status:** Accepted, 2026-11-15 (Wave 8c)

## Context

The project's north star ([`docs/VISION.md`](../VISION.md)) asks that stakeholders know the future
state of the business, that predictions show their track record, and that planning reacts to change
within 15 minutes. After Wave 8b the ERP knows forecasts, planned orders, supplier grades and
contracts, but not **what is at stake if a supplier fails**, nor **how good past forecasts were**,
and MRP ran only nightly.

## Decision

- **Revenue at risk per supplier** (`apps.planning.risk.supplier_risk`): a supplier's sources are its
  purchase orders, active contracts and preferred-supplier settings; a material with one source is
  single source; where-used through every active BOM finds the products that depend on it; open
  sales order lines for those products within the horizon are the supplier's exposed revenue, and
  those that depend on a single-source material its revenue at risk. A risk level (HIGH: revenue at
  risk with a C/D or ungraded supplier or no contract; MEDIUM: revenue at risk otherwise; LOW) and a
  readable recommendation (qualify a second source by RFQ, agree a contract, evaluate the supplier).
  The recommendation is a proposal; people decide.
- **Forecast accuracy** (`risk.forecast_accuracy`): each past month's forecast, taken from the run
  available at the start of that month, against actual deliveries; MAE, bias and accuracy
  (1 - total absolute error / total actual).
- **Computed in the ERP's service layer, not in Cypher.** The graph holds the same relationships,
  but a multi-level where-used path query cannot be tested outside CI's Neo4j service, while the
  calculation in Python is tested end to end and reads the system of record directly. The graph
  keeps its single-source view (`supplier_dependency`) for exploration and KG-RAG.
- **Read-only and always current:** both are calculated on request (API and `supply_risk` command),
  nothing is stored, so there is nothing to keep in step.
- **MRP every 15 minutes** instead of nightly: the planning latency target. Unchanged data returns the
  stored run (ADR 0015's fingerprint), so a quiet quarter of an hour costs one snapshot.

## Alternatives considered

- **Event-triggered MRP on each relevant outbox event:** faster, but needs debouncing to avoid a run
  per document; a 15-minute check meets the target with far less machinery.
- **Storing risk snapshots:** would allow trends, but adds a table to keep correct; the control tower
  (Wave 9) will decide which KPIs to keep over time.
- **A probabilistic risk model** (supplier failure likelihood × impact): more precise, but the data for
  failure probabilities does not exist yet; the rules are transparent and the grades already encode
  delivery performance.

## Consequences

- A planner sees, per supplier, which customer orders it could stop and what to do about it.
- Forecast quality is visible month by month, so trust in the plan can be earned or questioned.
- Planned orders can be superseded several times a day as data changes; converting an older one is
  refused with a message to use the latest run.
