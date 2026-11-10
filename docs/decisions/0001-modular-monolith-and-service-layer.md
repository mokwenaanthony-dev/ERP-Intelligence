# 0001. Modular monolith with business rules in a service layer

- **Status:** Accepted
- **Wave:** 0 (foundation)

## Context

An ERP's modules are tightly linked: a delivery changes stock, posts cost of sales and closes part of
a sales order, and all of it must succeed or fail together. The project is built by one developer,
so every extra deployable unit adds work without adding value.

## Decision

- All transactional modules (finance, inventory, sales, purchasing, HR, controlling) live in one
  Django/DRF application on one PostgreSQL database, as separate Django apps.
- Every business rule lives in the app's `services.py`. Views, the admin, management commands and
  other modules call the services; they never change business tables directly.
- A module becomes a separate service only when it has a different runtime profile, such as the
  planned FastAPI `ml_service` for forecasting.

## Alternatives considered

- **Microservices per module:** cross-module postings would need distributed transactions or
  sagas, and every service its own deployment, for no gain at this size.
- **Rules in serializers or models:** rules would be skipped by any path that does not use that
  serializer, and model `save()` hooks hide side effects.

## Consequences

- A sale, its stock movement and its journal entry commit in one database transaction.
- The same rules apply whether a document arrives through the API, a command or a test.
- Module boundaries are kept by convention (apps call each other's services), not by the network,
  so they must be watched in review.
