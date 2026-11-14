# 0012. CRM in the monolith, on the Business Partner; quotations become sales orders through the sales services

- **Status:** Accepted, 2026-11-12 (Wave 7a)

## Context

Wave 7 adds customer relationship management: campaigns, leads, opportunities, quotations and
after-sales service. CRM products are often separate systems (Salesforce, Dynamics) synchronised with
the ERP, which brings duplicate customer records and an interface to keep in step. CRM also holds
personal data of people who are not customers yet, which South Africa's POPIA protects.

## Decision

- **A `crm` app in the modular monolith** (ADR 0001), with its rules in `services.py`.
- **One customer record:** converting a lead creates a **Business Partner** with a customer role in
  the company code (or uses an existing partner), the same record sales, finance and the graph use.
  No separate CRM "account" or "contact" master.
- **Quotation to order through the sales services:** accepting a quotation calls
  `apps.sales.services.create_sales_order` with the **quoted prices passed explicitly**, so the agreed
  price holds even if the price list changed, and every sales rule (customer role, plant, tax code)
  runs as for an order entered directly. The quotation keeps a link to its sales order, the sales
  order carries the quotation number as customer reference, and the graph's document flow starts at
  the quotation.
- **Every transition is idempotent** (ADR 0010): converting, sending, accepting, losing, resolving
  twice gives the first result; a transition that does not fit the status is refused with the reason.
- **Personal data is need-to-know:** lead contact names, e-mail addresses and phone numbers are shown
  only to the sales team and administrators (others get `null`), never written to the log, and leads
  are not projected into the graph. Ticket descriptions and resolutions (free text) stay out of the
  graph too. Opportunities, quotations, tickets and campaigns are projected without personal data.
- **Service level:** a ticket's priority sets the time it is due by (URGENT 4 h, HIGH 8 h, MEDIUM 24 h,
  LOW 72 h); reopening keeps the original due time, so the report shows real lateness.

## Alternatives considered

- **A separate CRM service or product:** a second customer master to synchronise, for one developer.
- **Accounts and contacts as CRM-only models:** duplicates the Business Partner and splits Customer 360.
- **Re-pricing accepted quotations from the price list:** simpler, but breaks the agreed price.
- **Leads in the graph:** useful for campaign analysis, but personal data in a second store that AI
  agents read; campaign performance is answered from the relational tables instead.

## Consequences

- A lead's company becomes a real customer at conversion: it can be invoiced at once.
- Customer 360 now shows the pipeline and service history beside orders and billing.
- Contact persons of existing customers (several people per company) are not modelled yet; a later
  wave can add them to the Business Partner, under the same personal-data rules.
