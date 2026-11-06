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
| 2 | Stock-movement ledger, Order-to-Cash, Procure-to-Pay | Next |
| 3 | HR and payroll posting, cost and profit centres | Planned |
| 4 | Analytics layer and BI dashboards | Planned |
| 5-7 | CRM and SRM, SCM with ML demand forecasting, PLM | Planned |

## What Wave 0 delivers

| Area | What exists |
|---|---|
| Identity | Custom `User` with 8 roles; JWT login, refresh and logout (blacklist) |
| Access control | One permission class per module with read and write role lists (matrix below) |
| Audit | Every record has `created_at`, `updated_at`, `created_by`, `updated_by`, filled automatically by the API and the admin |
| Inventory | Products with non-negative stock and price; products used on orders cannot be deleted (409) |
| Sales | Customers; sales orders with lines, price captured at time of sale, status rules, stock checks |
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

Purchasing and HR are empty apps reserved for Waves 2-3. Sales customers and inventory products move onto
Business Partner and Material in Wave 2.

## Role matrix

| Role | Writes | Reads only |
|---|---|---|
| Finance Manager | finance (incl. configuration, period open/close), currencies and rates, business partners | sales, inventory |
| Accountant | journal entries, business partners | finance configuration, sales, inventory |
| Sales Representative | sales (orders, customers), business partners | inventory (catalogue) |
| Inventory Manager | inventory, materials | - |
| Purchasing Officer | purchasing, business partners, materials | inventory |
| HR Admin | hr | - |
| Viewer | - | sales, inventory |
| Administrator / superuser | everything, including organisation structure | - |

Every signed-in user can read organisation structure and master data.

## Sales order rules

```
PENDING --> CONFIRMED --> SHIPPED
   |            |
   +--> CANCELLED <--+
```

- Confirming takes the ordered quantities out of stock, or fails with the list of shortages and changes nothing.
- Cancelling a confirmed order puts the stock back.
- Lines and customer can only change while the order is PENDING.
- Orders are never deleted; cancel them instead.
- Order and lines are saved in one database transaction.

Change status with `PATCH /api/sales/orders/<id>/ {"status": "CONFIRMED"}`.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health/` | No login needed |
| POST | `/api/auth/token/` | `{"username", "password"}` gives `access` + `refresh` |
| POST | `/api/auth/token/refresh/` | `{"refresh"}` |
| POST | `/api/auth/logout/` | `{"refresh"}`, blacklists it |
| GET, POST | `/api/inventory/products/` | |
| GET, PUT, PATCH, DELETE | `/api/inventory/products/<id>/` | |
| GET, POST | `/api/sales/customers/` | |
| GET, PUT, PATCH, DELETE | `/api/sales/customers/<id>/` | |
| GET, POST | `/api/sales/orders/` | Lines can be sent in the same request |
| GET, PUT, PATCH | `/api/sales/orders/<id>/` | No DELETE |
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

Send `Authorization: Bearer <access>` on every call except health and auth. A Postman collection is in `postman/`.

## Run it

Python 3.12 or 3.13 is recommended (Django 5.2 LTS).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt

copy .env.example .env            # macOS/Linux: cp; then set DJANGO_SECRET_KEY
python manage.py migrate
python manage.py seed_data            # one user per role, password ChangeMe123!
python manage.py setup_demo_company   # company 1000 with finance set-up and sample postings
python manage.py runserver
```

Full stack (PostgreSQL, Redis, Celery) with Docker:

```bash
docker compose up --build
docker compose exec web python manage.py seed_data
docker compose exec web python manage.py setup_demo_company
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
  finance/     Chart of accounts, fiscal calendar, tax codes, Universal Journal;
               services.py holds the posting rules; setup_demo_company command
  inventory/   Products
  sales/       Customers, orders, lines; services.py holds the order rules
  purchasing/ hr/    Reserved for Waves 2-3
erp_system/    Settings (settings_test.py for pytest), URLs, Celery
tests/         pytest suite
```

## Licence

MIT. See [LICENSE](LICENSE).
