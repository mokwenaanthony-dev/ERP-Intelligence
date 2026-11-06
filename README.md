# ERP-Intelligence

[![CI](https://github.com/mokwenaanthony-dev/ERP-Intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/mokwenaanthony-dev/ERP-Intelligence/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![Django](https://img.shields.io/badge/django-5.2%20LTS-0C4B33)
![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

A modular ERP platform built on Django/DRF, PostgreSQL, Redis and Celery, growing in tested waves
toward an SAP-style suite: core ERP (Financials, Logistics, HR), then extended operations
(CRM, SRM, SCM, PLM) and a columnar analytics layer.

## Roadmap

| Wave | Scope | Status |
|---|---|---|
| 0 | Foundation: roles and permissions, audit trail, sales orders with stock rules, request logging, tests, CI | Done |
| 1 | Org structure, Business Partner, Material master, Finance core (Universal Journal, fiscal periods, VAT) | Done |
| 2a | Stock-movement ledger: movement types, moving-average valuation, automatic journal postings, stock reconciliation | Done |
| 2b | Order-to-Cash on Business Partner and Material: order, delivery, invoice, payment, open items and ageing | Done |
| 2c | Procure-to-Pay: purchase order, goods receipt, supplier invoice with three-way match, payment | Next |
| 3 | HR and payroll posting, cost and profit centres | Planned |
| 4 | Analytics layer and BI dashboards | Planned |
| 5-7 | CRM and SRM, SCM with ML demand forecasting, PLM | Planned |

## What Wave 0 delivers

| Area | What exists |
|---|---|
| Identity | Custom `User` with 8 roles; JWT login, refresh and logout (blacklist) |
| Access control | One permission class per module with read and write role lists (matrix below) |
| Audit | Every record has `created_at`, `updated_at`, `created_by`, `updated_by`, filled automatically by the API and the admin |
| Inventory and sales | Simple products, customers and orders (retired in Wave 2b; see release `v0.1.0`) |
| Telemetry | `OperationalPerformanceMiddleware`: one line per request to stdout and a rotating 10 MB x 5 file. Paths only, never query strings or bodies |
| Ops | `/api/health/`, gunicorn + WhiteNoise, non-root Docker image with healthcheck, Compose stack |
| Quality | pytest suite with an 80% coverage floor; GitHub Actions: flake8, bandit, pip-audit, migrations check, tests on PostgreSQL, Docker build |

## What Wave 1 delivers

| Area | What exists |
|---|---|
| Organisation | Company codes (own currency, chart of accounts, fiscal-year start month), plants, storage locations, sales and purchasing organisations |
| Master data | Currencies and dated exchange rates (inverse rates used automatically), units of measure, Business Partner with per-company customer and supplier roles, Material master |
| Chart of accounts | G/L accounts by type; reconciliation accounts for receivables and payables that only accept postings with a matching business partner |
| Fiscal calendar | Fiscal years named by the year they end in (FY2027 = March 2026 to February 2027), 12 posting periods each, opened and closed by the Finance Manager |
| VAT | Tax codes per chart (15% output and input, zero-rated, exempt); the tax line is calculated and added automatically, rounded half-up |
| Universal Journal | One line-item table for every posting, modelled on S/4HANA's ACDOCA: company code, period, account, partner, material, plant, tax code, document and company currency, signed amounts, no stored totals |
| Posting rules | Balanced debits and credits, open period, valid account and partner, gap-free document numbers per company code, fiscal year and document type, all in one transaction |
| Corrections | Posted documents cannot be edited or deleted; reversal posts a mirror document and links the two |
| Reporting | Trial balance computed straight from the journal |
| Demo data | `python manage.py setup_demo_company` builds a South African company: ZAR, chart CAZA, VAT codes, fiscal year, partners, materials and two sample postings |

### Posting a journal entry

```http
POST /api/finance/journal-entries/
{
  "company_code": "1000",
  "document_type": "DR",
  "posting_date": "2026-05-15",
  "reference": "INV-0042",
  "lines": [
    {"gl_account": "1100", "debit_credit": "D", "amount": "2300.00", "business_partner": "1000001"},
    {"gl_account": "4000", "debit_credit": "C", "amount": "2000.00", "tax_code": "O1"}
  ]
}
```

The response is document `DR00000002` with three lines: the 15% output VAT line (R300.00 to account 2100)
is added by the system.

## What Wave 2a delivers

| Area | What exists |
|---|---|
| Movement types | 561 initial stock, 101 goods receipt, 601 goods issue for delivery, 551 scrapping, 701/702 physical inventory gain/loss, 311 transfer between storage locations |
| Material documents | Immutable, numbered (`MD00000001`), reversible; items carry signed quantities and values per storage location |
| Valuation | Moving average price per material and plant, always computed from the movements (no stored totals); issuing all stock takes the full remaining value, so no cents are left behind |
| Accounting | Every value-changing movement posts a journal entry in the same transaction (`WE` receipts, `WA` issues, `WI` count differences), with material and plant on each line |
| Account determination | Inventory, GR/IR clearing, initial entry, cost of sales, scrapping and inventory-difference accounts per chart, optionally per material type |
| Controls | Stock checked per storage location; closed periods blocked; reversals refused if the stock has already been issued or would leave value without quantity |
| Reports | Stock overview (quantity per storage location, value and moving average price per plant) and a stock-to-ledger reconciliation that proves stock value equals the inventory account balance |

### Posting a goods receipt

```http
POST /api/inventory/goods-movements/
{
  "movement_type": "101",
  "plant": "1000",
  "posting_date": "2026-05-15",
  "business_partner": "1000002",
  "items": [{"material": "TG-1001", "storage_location": "0001", "quantity": "50", "unit_price": "150.00"}]
}
```

Creates material document `MD00000002` and journal entry `WE00000002` (Dr 1200 Inventory R7,500 /
Cr 2300 GR/IR clearing R7,500). The moving average price of TG-1001 moves from R120 to R130.

## What Wave 2b delivers

```
Sales order --> Delivery --------------> Invoice --------------------------> Payment
 (prices,       601 goods issue:          Dr Receivables (gross)             Dr Bank
  VAT code)     Dr Cost of sales          Cr Revenue (net) / Cr VAT output   Cr Receivables
                Cr Inventory (moving avg)                                     clears the open item
```

| Area | What exists |
|---|---|
| Pricing | Sales prices per material and sales organisation with validity dates; manual price override flagged on the item |
| Sales orders | On Business Partner (customer role) and Material; VAT code defaulted per material type; OPEN (editable) -> CONFIRMED -> COMPLETED, or CANCELLED; delivered and invoiced quantities derived from the documents |
| Deliveries | Full or partial, per plant; post a 601 goods issue that books cost of sales at moving average; reversible until invoiced |
| Invoices | One per delivery: receivable (gross), revenue (net) and output VAT per item, due date from the customer's payment terms; cancellable while unpaid |
| Payments and open items | Incoming payments clear receivables (explicitly or oldest first, partial allowed); reversing a payment re-opens its items; open-item list per customer |
| Reports | Receivables ageing (not due, 1-30, 31-60, 61-90, over 90 days) and a receivables-to-ledger reconciliation |
| Numbering | `SO`, `DL` and `IN` numbers run continuously per company code; accounting documents keep their yearly `WA`, `DR`, `DZ` ranges |

The Wave 0 sales `Customer`/`SalesOrder` and inventory `Product` models were retired in this wave
(migration `sales/0002_retire_wave0_sales_models`). Purchasing and HR are reserved for Waves 2c and 3.

### Order to cash in five calls

```http
POST /api/sales/orders/                     {"sales_organization": "1000", "customer": "1000001", "order_date": "2026-05-10",
                                             "items": [{"material": "TG-1001", "quantity": "10"}, {"material": "TG-1002", "quantity": "2"}]}
POST /api/sales/orders/1/confirm/
POST /api/sales/orders/1/deliver/           {"delivery_date": "2026-05-12", "storage_location": "0001"}
POST /api/sales/invoices/                   {"delivery": 1, "invoice_date": "2026-05-15"}
POST /api/finance/incoming-payments/        {"company_code": "1000", "customer": "1000001", "payment_date": "2026-06-10",
                                             "amount": "3436.20", "bank_account": "1000"}
```

Result: order `SO00000001` (R2,988.00 net), delivery `DL00000001` with goods issue `WA00000001`
(cost of sales R1,800), invoice `IN00000001` / `DR00000002` (R2,988.00 + R448.20 VAT = R3,436.20),
payment `DZ00000001` clearing the oldest open items first.

## Role matrix

| Role | Writes | Reads only |
|---|---|---|
| Finance Manager | finance (incl. configuration, period open/close, payments), invoices, currencies and rates, business partners | sales orders, deliveries, inventory |
| Accountant | journal entries, payments, invoices, business partners | finance configuration, sales orders, deliveries, inventory |
| Sales Representative | sales orders, prices, deliveries, invoices, business partners | inventory |
| Inventory Manager | goods movements, delivery reversal, materials | deliveries |
| Purchasing Officer | purchasing, business partners, materials | inventory |
| HR Admin | hr | - |
| Viewer | - | sales orders, deliveries, invoices, inventory |
| Administrator / superuser | everything, including organisation structure | - |

Every signed-in user can read organisation structure and master data.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health/` | No login needed |
| POST | `/api/auth/token/` | `{"username", "password"}` gives `access` + `refresh` |
| POST | `/api/auth/token/refresh/` | `{"refresh"}` |
| POST | `/api/auth/logout/` | `{"refresh"}`, blacklists it |
| CRUD | `/api/sales/prices/` | `?material=` |
| GET, POST | `/api/sales/orders/` | Filters `status`, `customer` |
| GET, PATCH | `/api/sales/orders/<id>/` | PATCH `{"items": [...]}` replaces items while OPEN; no PUT or DELETE |
| POST | `/api/sales/orders/<id>/confirm/`, `/cancel/` | |
| POST | `/api/sales/orders/<id>/deliver/` | `{"delivery_date", "storage_location"}` or `{"delivery_date", "items": [{"item_number", "quantity", "storage_location"}]}` |
| GET | `/api/sales/deliveries/` | `?sales_order=` |
| POST | `/api/sales/deliveries/<id>/reverse/` | Until invoiced |
| GET, POST | `/api/sales/invoices/` | POST `{"delivery", "invoice_date"}`; filters `customer`, `status` |
| POST | `/api/sales/invoices/<id>/cancel/` | While unpaid |
| CRUD | `/api/org/company-codes/<code>/`, `plants/<code>/`, `storage-locations/`, `sales-organizations/<code>/`, `purchasing-organizations/<code>/` | |
| CRUD | `/api/masterdata/currencies/<code>/`, `exchange-rates/`, `units/<code>/` | |
| CRUD | `/api/masterdata/business-partners/<number>/` | `?search=` by name; number assigned by the system |
| CRUD | `/api/masterdata/customer-roles/`, `supplier-roles/` | Reconciliation account given by its number |
| CRUD | `/api/masterdata/materials/<number>/` | |
| CRUD | `/api/finance/charts-of-accounts/<code>/`, `gl-accounts/`, `tax-codes/` | |
| GET, POST | `/api/finance/fiscal-years/` | POST creates the year and its 12 periods |
| GET | `/api/finance/periods/` | `?company_code=&fiscal_year=` |
| POST | `/api/finance/periods/<id>/open/`, `/close/` | Finance Manager |
| GET, POST | `/api/finance/journal-entries/` | POST posts; filters `company_code`, `fiscal_year`, `period`, `document_type`, `status` |
| POST | `/api/finance/journal-entries/<id>/reverse/` | Optional `{"posting_date"}` |
| GET | `/api/finance/reports/trial-balance/` | `?company_code=1000&fiscal_year=2027[&period_from=&period_to=]` |
| POST | `/api/finance/incoming-payments/` | Optional `allocations: [{"line_id", "amount"}]`; otherwise oldest first |
| GET | `/api/finance/reports/open-items/` | `?company_code=&business_partner=&include_cleared=` |
| GET | `/api/finance/reports/receivables-ageing/` | `?company_code=&as_of=` |
| GET | `/api/finance/reports/receivables-reconciliation/` | `?company_code=` |
| GET, POST | `/api/inventory/goods-movements/` | POST posts; filters `plant`, `movement_type`, `material`, `status` |
| POST | `/api/inventory/goods-movements/<id>/reverse/` | Optional `{"posting_date"}` |
| GET | `/api/inventory/stock/` | `?plant=&material=` |
| GET | `/api/inventory/reports/stock-reconciliation/` | `?company_code=1000` |
| CRUD | `/api/inventory/account-settings/` | Account determination (Finance Manager) |

Send `Authorization: Bearer <access>` on every call except health and auth. A Postman collection is in `postman/`.

## Run it

Python 3.12 or 3.13 is recommended (Django 5.2 LTS).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt

copy .env.example .env            # macOS/Linux: cp; then set DJANGO_SECRET_KEY
python manage.py migrate
python manage.py seed_data            # one user per role (password ChangeMe123!) + demo company 1000
python manage.py runserver
```

Full stack (PostgreSQL, Redis, Celery) with Docker:

```bash
docker compose up --build
docker compose exec web python manage.py seed_data
```

## Test and check

```bash
pytest                                                       # tests + coverage (fails below 80%)
flake8 .
bandit -r apps erp_system -ll -x "*/migrations/*"
pip-audit -r requirements.txt
python manage.py makemigrations --check --dry-run --settings=erp_system.settings_test
```

The same gates run in `.github/workflows/ci.yml` on every push and pull request.

## Project layout

```
apps/
  accounts/    User, roles, permission classes
  common/      Audit base models, middleware, shared view/admin mixins, seed_data
  organization/  Company codes, plants, storage locations, sales and purchasing organisations
  masterdata/  Currencies, exchange rates, units, Business Partner and roles, Material master
  finance/     Chart of accounts, fiscal calendar, tax codes, Universal Journal, open items and
               payments; services.py holds the posting rules; setup_demo_company command
  inventory/   Stock ledger: material documents, valuation, account determination
  sales/       Prices, sales orders, deliveries, invoices; services.py holds order-to-cash
  purchasing/ hr/    Reserved for Waves 2c and 3
erp_system/    Settings (settings_test.py for pytest), URLs, Celery
tests/         pytest suite
```

## Licence

MIT. See [LICENSE](LICENSE).
