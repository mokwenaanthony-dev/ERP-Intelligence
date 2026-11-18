# 0017. The control tower calculates KPIs and gathers problems on request, from the modules that already know them

- **Status:** Accepted, 2026-11-15 (Wave 9a)

## Context

The north star ([`docs/VISION.md`](../VISION.md)) asks that stakeholders know the state and the future
state of the business in real time, and names the results the system should move: on time in full
(to customers and from suppliers), forecast accuracy, inventory days and revenue at risk. After Wave
8c each of these could be worked out somewhere, and each module detected its own problems (cells
down, MRP messages, supply risks, overdue tickets), but nobody saw them together.

## Decision

- **A read-only `control` app** with two answers: `kpis` and `problems`.
- **KPIs with a target, a status and their basis.** Each KPI says how it was measured ("1 of 2 order
  lines due in the last 90 days"), so a number can be checked. Targets live in
  `settings.CONTROL_TARGETS`; status rules are simple and written down (good / warning / critical).
  A KPI with nothing to measure shows "no data", never a guessed zero.
- **On time needs a promised date** (v0.19.1): customer on-time-in-full counts only order lines with
  a requested delivery date. The first version fell back to the order date, which made every
  undated order "late" (0 of 76 in the Docker demo); lines without a date are now counted
  separately and named in the basis.
- **The future state beside them:** the open order book for the coming months, planned orders that
  should already have started, suppliers at high risk.
- **A problems inbox from existing detectors**, not new rules: cell state (UNS), MRP messages, supplier
  risk (8c), overdue tickets (CRM), late customer order lines and overdue supplier deliveries. Each
  problem has an area, a severity, a document, an owner role and a stable key, so Wave 9b can record
  a decision about it.
- **Role-filtered:** the inbox shows only the areas a role can read elsewhere in the API; HR sees none.
- **Calculated on request, nothing stored:** always current and nothing to keep in step; the
  latency is that of the data underneath (seconds for cell state, minutes for MRP).

## Alternatives considered

- **A Metabase dashboard only:** good for trends, but it cannot apply the ERP's roles row by row or
  carry problem keys for decisions; it can be added on top of the same numbers later.
- **Storing KPI snapshots daily:** gives history and trends; deferred until it is clear which ones
  people watch, so no table has to be kept correct before it is needed.
- **New detection rules in the control tower:** would duplicate the modules' own logic; the tower
  only gathers.

## Consequences

- One request shows how the business is doing and what to do next, with an owner for each problem.
- Every number traces back to the documents it was counted from.
- KPI history and charts over time, and decisions on problems and recommendations, come next (9b).
