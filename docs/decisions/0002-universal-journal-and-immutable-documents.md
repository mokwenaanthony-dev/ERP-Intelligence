# 0002. One Universal Journal; posted documents are immutable

- **Status:** Accepted
- **Wave:** 1 (Financials)

## Context

In older ERPs each module kept its own ledger, and month end was spent reconciling them. SAP
S/4HANA replaced them with one line-item table (ACDOCA) that every module posts to. Auditors also
expect posted documents never to change.

## Decision

- One journal-line table receives the financial effect of every module, with the business
  dimensions on each line: business partner, material, plant, cost centre, profit centre.
- No stored running totals: balances, open items and stock are sums over signed line items.
- Posted documents cannot be edited or deleted. A mistake is corrected by a reversal document that
  points to the original.
- Accounting documents are numbered gap-free per company code, fiscal year and document type, using
  a row lock.

## Alternatives considered

- **Separate sub-ledgers per module:** the reconciliation problem the Universal Journal removes.
- **Stored balances updated on every posting:** faster reads, but a second truth that can drift
  and must be locked on every posting.
- **Editable documents with an audit log:** harder to audit, and every report would need to know
  which version it read.

## Consequences

- Stock, receivables, payables and the general ledger reconcile by construction; tests prove it.
- Reports and the analytics layer read one table with every dimension already present.
- Reversals add rows rather than changing them, so every correction is visible.
- Summing line items gets slower as volume grows; that is what the analytics layer (ADR 0003) is for.
