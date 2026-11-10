# 0008. Money and quantities in API responses are exact decimal strings

- **Status:** Accepted, Wave 5b (proposed 2026-11-10 during Wave 5a)

## Context

Found while trying the Wave 5a cost estimate: amounts came back as `16425.0` and `1.5`. Checking
the rest of the API showed two different representations of the same kind of value:

- Endpoints built on DRF serializers (orders, invoices, BOM items) return decimals as **strings**
  with their exact scale, e.g. `"450.00"`, `"1.200"`. DRF does this by default.
- Endpoints that return a service result directly (`Response(services.trial_balance(...))`) return
  **floats**, because DRF's JSON encoder converts `Decimal` to `float`. This affects the trial
  balance, open items, stock overview and reconciliation, every analytics report and the cost
  estimate, and has done since Wave 1.

Floats cannot represent most cent amounts exactly (0.1 + 0.2 ≠ 0.3), they lose the scale (`450.00`
becomes `450.0`), and large values can lose digits. Inside the ERP all money is `Decimal`; the
problem is only at the API boundary, but that is where other systems (the MES, BI tools, AI agents)
read the numbers.

## Decision

- Every `Decimal` in every API response is rendered as a **string with its exact scale**: money with
  two decimals, quantities with three, prices and rates as stored.
- Implemented once, at the boundary: `apps/common/renderers.py`, a JSON renderer whose encoder
  writes `Decimal` with all its digits (`format(value, "f")`, never exponent notation), set as the
  default renderer in `REST_FRAMEWORK`. Services keep returning `Decimal`; no view changes.
- A test pins it: report endpoints return strings such as `"22525.00"` in the rendered JSON.

## Alternatives considered

- **Leave floats in reports:** simpler, but two representations of money in one API, and inexact
  values handed to other systems.
- **Integers in cents:** exact and common in payment APIs, but quantities need three decimals and
  rates four, so one rule (exact decimal strings) is simpler than several.
- **Serializers for every report:** also gives strings, but a lot of boilerplate for read-only
  dictionaries; the renderer covers them all.

## Consequences

- A client must parse amounts as decimals (`Decimal("22525.00")` in Python, a decimal library in
  JavaScript) rather than use JSON numbers. This is a breaking change for any client that relied on
  numbers; there are none yet outside this project, which is why it is done now.
- Metabase is not affected: it reads the reporting views with SQL, not the API.
- The knowledge graph still stores floats (Neo4j has no decimal type, ADR 0004); the ERP remains the
  record for exact amounts.
