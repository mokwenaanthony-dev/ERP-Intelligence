# ERP-Intelligence: Modular ERP Platform

A modular-monolith ERP built on Django/DRF, PostgreSQL, Redis and Celery, growing in waves toward an
SAP-style suite: core ERP (Logistics, Financials, HR), then extended operations (CRM, SRM, SCM, PLM)
and a columnar analytics layer. The full roadmap is in the ERP-Intelligence project (`roadmap/ERP_Roadmap.md`).

## Status: Wave 0 (foundation) complete

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

Finance, Purchasing and HR are empty apps reserved for Waves 1-3.

## Role matrix

| Role | Writes | Reads only |
|---|---|---|
| Finance Manager, Accountant | finance | sales, inventory |
| Sales Representative | sales (orders, customers) | inventory (catalogue) |
| Inventory Manager | inventory | - |
| Purchasing Officer | purchasing | inventory |
| HR Admin | hr | - |
| Viewer | - | sales, inventory |
| Administrator / superuser | everything | - |

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

Send `Authorization: Bearer <access>` on every call except health and auth. A Postman collection is in `postman/`.

## Run it

Python 3.12 or 3.13 is recommended (Django 5.2 LTS).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt

copy .env.example .env            # macOS/Linux: cp; then set DJANGO_SECRET_KEY
python manage.py migrate
python manage.py seed_data        # one user per role, password ChangeMe123!
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

The same gates run in `.github/workflows/ci.yml` on every push and pull request once this folder is
its own Git repository on GitHub.

## Project layout

```
apps/
  accounts/    User, roles, permission classes
  common/      Audit base models, middleware, shared view/admin mixins, seed_data
  inventory/   Products
  sales/       Customers, orders, lines; services.py holds the order rules
  finance/ purchasing/ hr/    Reserved for Waves 1-3
erp_system/    Settings (settings_test.py for pytest), URLs, Celery
tests/         pytest suite
```
