# Side by side: loading data from the ERP you already have

You do not have to replace your ERP to use this system's intelligence: forecasting, MRP, the
control tower, the supplier and customer portal. Export your master data and open documents as
CSV files, load them here, and keep running your ERP as the system of record (ADR 0020). Load
again whenever you like: what was loaded before is recognised by your ERP's own keys and is not
loaded twice.

```bash
docker compose exec web python manage.py import_data --company 1000 --object partners --file docs/import-samples/partners.csv
```

Or through the API: `POST /api/connect/imports/` with the fields `company_code`, `source`, `object` and
`file`. `GET /api/connect/sources/` lists the columns of every object, and the results of each row
are in `GET /api/connect/imports/<id>/`. Only the finance manager (and administrators) may import.

## How a load behaves

- **Through the same rules as everything else.** A row goes in through the services the API uses,
  so it meets every business rule. A row that breaks one is **rejected with the rule's own message**
  ("Partner 'S-0202' (1000010) is not a customer in 1000."); the other rows still load. Each order
  loads in its own transaction: all of its lines, or none.
- **The same file again changes nothing that loaded.** It returns the first load's result (200 in
  the API, "Already loaded" from the command). If that load rejected rows, **only those rows are
  tried again**, because their cause may have been fixed since: the partners they needed have now
  been loaded, or a period has been opened. Rows that loaded are never applied a second time, so a
  retry cannot post stock twice or undo what happened since.
- **Each row is matched by your key.** It is matched by `external_id` (or `order`, or `material`
  and `plant` for stock) to what was loaded before. An unchanged row is `unchanged`; a changed
  partner or material is `updated`. An order loaded before is never created twice. A changed
  order is refused, saying so: change it here, or cancel it and load it again.
- **Existing records are reused, not duplicated.** A partner with the VAT number of one we already
  have is linked to it. A material whose number we already use is linked to that material.
- **Stock is a snapshot.** The load posts only the difference from what we hold: an initial entry
  (561) for a material's first stock at a plant, and a count gain or loss (701 or 702) after that,
  each with its journal entry. The same snapshot again posts nothing.
- **Load in this order:** partners, materials, stock, purchase_orders, sales_orders. Orders refer to
  partners and materials by your keys, and are refused until those have been loaded.
- **Not loaded:** goods receipts, deliveries, invoices and payments. The books stay your ERP's job;
  this system plans and advises on top of them.

## Files

UTF-8 CSV with a header row. The separator is a comma or a semicolon; Excel's byte-order mark and
spaces around values are fine. Dates are `2026-11-28` (or `28/11/2026`), decimals use a full stop
(`4.40`). Column names are as below, in any order; extra columns are ignored. Working samples, an
invented company's export, are in [`docs/import-samples/`](import-samples/), each with one row
that is rejected on purpose.

### partners

| Column | Required | Meaning |
|---|---|---|
| `external_id` | yes | Your ERP's customer or supplier code |
| `name` | yes | |
| `role` | yes | `CUSTOMER`, `SUPPLIER` or `BOTH` |
| `vat_number` | | Used to link to a partner we already have |
| `registration_number`, `city` | | |
| `country` | | Two letters, default `ZA` |
| `payment_terms_days` | | Default 30 |

### materials

| Column | Required | Meaning |
|---|---|---|
| `external_id` | yes | Your ERP's item code |
| `description` | yes | |
| `unit` | yes | A unit we know (`EA`, `KG`, `L`, `HR`, `BOX`); a mapping translates yours |
| `type` | | `HAWA` trading goods (default), `FERT` finished, `HALB` semi-finished, `ROH` raw, `DIEN` service |
| `number` | | Our material number (up to 18 characters); default: `external_id` |
| `group`, `plant` | | A plant code creates the material's plant view |

### stock

| Column | Required | Meaning |
|---|---|---|
| `material` | yes | The material's `external_id` |
| `plant` | yes | Our plant code (a mapping translates your site codes) |
| `quantity` | yes | On hand, in the base unit |
| `unit_price` | for a first stock | Value per unit, for the initial entry |
| `storage_location` | | Default: the plant's first |
| `as_of` | | Posting date, default today (its period must be open) |

### purchase_orders and sales_orders

One row per order line; the lines of an order share its `order`, partner and `order_date`.

| Column | Required | Meaning |
|---|---|---|
| `order` | yes | Your ERP's order number (kept as the supplier or customer reference) |
| `line` | yes | Line number in your ERP |
| `supplier` / `customer` | yes | The partner's `external_id` |
| `material` | yes | The material's `external_id` |
| `quantity` | yes | The **open** quantity (still to be received or delivered) |
| `price` | yes | Net price per unit (kept as given, not taken from our price list) |
| `order_date` | yes | |
| `delivery_date` / `requested_date` | | When it is due |
| `plant` | | Default: the first plant |

Purchase orders are loaded released, as they are in your ERP; sales orders are loaded open.

## Mappings: your ERP's own export files

`generic` is the layout above. A **mapping** reads another ERP's own export unchanged. It names
which column holds each field (`BPCNUM` for `external_id`) and how codes translate (unit `UN` to
`EA`, site `JHB` to plant `1000`, role `1` to `CUSTOMER`). It also gives the date and decimal style
(`28.11.2026`, `1.250,50`). See `apps/connect/mappings.py`; a test proves the layer with such a
mapping.

A Sage X3 mapping (customers BPC, suppliers BPS, products ITM, open orders) will be built and tested
against a real X3 instance's SEED demo data, not guessed from documentation.
