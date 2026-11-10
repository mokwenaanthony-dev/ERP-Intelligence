# 0003. Analytics and graph are read models fed by a transactional outbox

- **Status:** Accepted
- **Waves:** 4a (analytics), 4b (graph)

## Context

Aggregations over time (P&L by period, margins) and relationship questions (which customers depend
on this supplier) are slow or awkward on the transactional tables. Copying data to other stores
risks the copies disagreeing with the books.

## Decision

- The relational core is the only system of record. The analytics views and the knowledge graph
  are **read models**: rebuilt from the core, never written to directly.
- Analytics: plain SQL views, materialized on PostgreSQL and refreshed concurrently by Celery beat.
- Graph: every change to a projected record writes an `OutboxEvent` row **in the same database
  transaction** as the change. A Celery worker reads the outbox and updates Neo4j.
- Projection is state-based: the worker re-reads the record's current state and MERGEs it, so
  duplicate or out-of-order events are harmless and `graph_rebuild` can recreate the graph at any time.

## Alternatives considered

- **Dual writes** (write to PostgreSQL and Neo4j in the same request): if the second write fails,
  the stores disagree with no record of it.
- **Change data capture** (e.g. Debezium on the PostgreSQL log): robust, but more infrastructure
  than one developer should run at this stage; the outbox can be moved to CDC later unchanged.
- **Event sourcing for the whole ERP:** powerful but a much larger change; immutable documents
  (ADR 0002) already give an append-only history.

## Consequences

- If a posting rolls back, its event rolls back too; the graph can lag, but never invent data.
- Neo4j can be down without affecting postings; events wait and are processed later.
- The same outbox will feed the Unified Namespace in Wave 6 (ADR 0007), so the ERP still writes to
  only one place per transaction.
