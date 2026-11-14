# 0013. SRM in the monolith: onboarding creates the Business Partner; awards and releases are purchase orders through the purchasing services

- **Status:** Accepted, 2026-11-14 (Wave 7b)

## Context

Wave 7b adds supplier relationship management: onboarding new suppliers, requests for quotation
(RFQs), contracts (outline agreements) and supplier evaluation. As with CRM (ADR 0012), SRM products
are often separate systems (SAP Ariba, Coupa) synchronised with the ERP. Onboarding also collects
personal data of the applicant's contact person, which POPIA protects, and South African purchasing
records the supplier's B-BBEE contributor level.

## Decision

- **An `srm` app in the modular monolith** (ADR 0001), with its rules in `services.py`.
- **Onboarding creates the one supplier record:** approving an application needs a company
  registration number and a VAT number (due diligence). It creates a **Business Partner** with a
  supplier role on the payables reconciliation account, or reuses the partner already on file with
  the same VAT number, so a company never appears twice. Rejections keep their reason.
- **Award and release through the purchasing services:** awarding an RFQ calls
  `apps.purchasing.services.create_purchase_order` at the **quoted prices**, with the delivery date
  from the quoted delivery time; releasing against a contract does the same at the **contract
  prices**, only within the contract's validity and never beyond its target quantity per material
  (the contract row is locked, so two releases cannot both take the last units). Cancelled purchase
  orders give their quantity back. Every purchasing rule (supplier role, plant, tax code) runs as for
  an order entered directly.
- **At least two suppliers per RFQ**, so there is something to compare; the comparison ranks complete
  quotes by total and delivery time and shows the lowest price per item.
- **Evaluation from real documents:** on time (posted goods receipts on or before the order's
  delivery date), in full (received against ordered per item, capped at 100 %), price (invoice items
  without a price difference), plus a quality score 1-5 from the users of the goods. Weights 40/20/20/20;
  a measure with nothing to measure is left out and the others weighted up. Grades A ≥ 90, B ≥ 75,
  C ≥ 60, otherwise D. One row per supplier and period: evaluating again replaces it.
- **Every operation is idempotent** (ADR 0010): approving, rejecting, sending, awarding, cancelling
  twice gives the first result; resubmitting an unchanged quote writes nothing; a transition that
  does not fit the status is refused with the reason.
- **Personal data is need-to-know:** the applicant's contact name, e-mail address and phone number
  are shown only to purchasing and administrators (others get `null`), never logged, and applications
  are not projected into the graph. RFQs, contracts and the latest grade (on the supplier's node) are.

## Alternatives considered

- **A separate SRM product or service:** a second supplier master to synchronise, for one developer.
- **Contracts as a purchase order type (SAP's scheduling agreement):** fewer tables, but target
  quantities, validity and releases would leak into every purchase order rule.
- **Evaluation from questionnaires only:** easy, but opinion; measured delivery and price
  performance from receipts and invoices is what purchasing can act on.
- **Storing every evaluation on the graph:** the latest grade is what risk queries need; the history
  stays in the relational table and the scorecard.

## Consequences

- Supplier dependency on the graph now shows each source's grade and whether an active contract
  covers the material, so "single source, no contract, grade D" stands out as a supply risk.
- Customer 360 shows, for a supplier, its grade, active contracts and open RFQs.
- The document flow starts at an RFQ or a contract and continues into the purchasing chain.
- Supplier portals (suppliers entering their own quotes) and contract price scales are not built yet;
  quotes are recorded by purchasing.
