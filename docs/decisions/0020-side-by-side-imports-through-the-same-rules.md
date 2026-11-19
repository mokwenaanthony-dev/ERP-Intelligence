# 0020. Side by side: another ERP's exports load through the same rules, idempotently, row by row

- **Status:** Accepted, 2026-11-18 (Wave 10b); amended 2026-11-19 in v0.22.1 (rejected rows are retried)

## Context

The project's positioning ([`docs/VISION.md`](../VISION.md)) is not to replace an ERP. It is to
show how an ERP becomes intelligent, in steps a company can adopt. "Nobody rips out an ERP for an
experiment" (switching cost, the third wall in Noel D'Costa's account of Klarna). So the
intelligence must be able to run **beside** the ERP a company already has: Sage X3, SAP or
anything else, with that ERP staying the system of record.

The north star also has a fair-competition safeguard: data shared outside the company only as
agreed with that partner. The partner portal of 10a broke it in one place. The demand outlook
showed a supplier every planned purchase of a material it supplies, including volumes a second
source may be given.

## Decision

- **Files, not a live connection.** Master data and open documents come in as CSV exports:
  partners, materials, stock, open purchase and sales orders. Every ERP can export a report; few
  small companies can open an API to their ERP. A live connector (an ERP's API, change data
  capture) can feed the same functions later.
- **Through the same rules.** Each row goes in through the services the API uses (purchase and
  sales orders, goods movements, partner roles), so imported data meets every business rule. A
  row that breaks one is rejected with the rule's own message, while the rest loads. Each row is
  its own transaction; each order is one transaction for all its lines.
- **Idempotent at two levels** (ADR 0010).
  - The file: its fingerprint (SHA-256 of company code, source, object and content) is unique, so
    the same file again returns the first batch.
  - The row: `ExternalReference` maps the source's key (customer `C-0101`) to our number. An
    unchanged row is `unchanged`; a changed partner or material is updated. An order is never
    created twice. A changed order is refused, saying so, rather than silently rewritten.
  - Existing records are linked, not duplicated: a partner by VAT number, a material by number.
- **Stock is a snapshot, posted as differences:** an initial entry (561) for a first stock, then
  count gains and losses (701 and 702), each with its journal entry, so the books still reconcile.
  The same snapshot again posts nothing.
- **The books stay with the other ERP.** Receipts, deliveries, invoices and payments are not
  loaded. This system plans, predicts and advises on what the other ERP holds; the people act in
  their ERP.
- **A mapping per source.** A `Mapping` names each source's column headings, code translations
  (units, sites, roles), date formats and decimal style. `generic` is the documented layout
  (`docs/IMPORT_FORMAT.md`). The Sage X3 mapping is built against a real X3 instance's SEED demo
  data, not guessed from documentation.
- **Fair competition in the outlook.** Planned purchases and the forecast are shown only for
  materials where the supplier is the preferred supplier or the only source. For any other
  material it sees what is ordered from it, marked `planned_shared: false`.
- **Imports are the finance manager's.** They change master data and stock across modules. The
  other business roles may read the batches. Partner roles are refused, like every internal route.

**v0.22.1.** In the first Docker demo, the purchase and sales order files were loaded before the
partners, so every order was rejected ("load partners first"). Loading them again after the
partners returned the first, all-rejected batch: the file-level rule was too strong, and those
orders could never load from that file. Now the same file again **retries only the rows that were
rejected**, in the same batch. Rows that loaded are never applied twice. A retry cannot post stock
again or bring it back to an old snapshot; a test posts a stock movement between two loads to
prove it.

## Alternatives considered

- **A live integration with each ERP (SAP OData, X3 web services):** fresher data, but one project
  per ERP, credentials to the customer's system, and a support burden. Files work for every ERP on
  day one, and the import functions are what a live feed would call.
- **Loading straight into the tables, skipping the services:** faster for big loads, but imported
  data would escape the business rules. The rules are the value of the system (the business-rules
  catalogue, 9b).
- **Updating loaded orders in place:** needed eventually, but changing a released purchase order
  under a supplier's confirmation needs its own rules. Refusing with a clear message is honest
  until they exist.
- **Stock as movements rather than snapshots:** exact, but most ERPs export stock on hand far more
  easily than a movement history. Differences keep both systems' totals in step.
- **Guessing a Sage X3 mapping from documentation:** quick, but likely wrong in the details (code
  lists, date formats, decimal styles). A real export is the test.

## Consequences

- A company can see its own data in the control tower, MRP and the portal within an afternoon,
  without changing its ERP. It can stop at any time, and nothing in its ERP changed.
- Every load is a record: what came in, what was rejected and why, who loaded it.
- The catalogue picked up the import's 26 rules by itself (318 in all).
- Not yet: updating loaded orders, a Sage X3 mapping (waiting for the SEED exports), a scheduled
  pick-up of files from a folder, and anonymised benchmarks across companies (Wave 10c).
