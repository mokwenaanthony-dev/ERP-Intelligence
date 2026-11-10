# Unified Namespace contract (version 1)

The agreement between **ERP-Intelligence** (ISA-95 Level 4) and **MES-Intelligence** (Level 3). Both
sides build against this document; it is the only thing they share
([ADR 0009](decisions/0009-mes-in-its-own-repository.md)). The conventions behind it are in
[ADR 0006](decisions/0006-isa-95-equipment-hierarchy-as-shared-vocabulary.md) (vocabulary) and
[ADR 0007](decisions/0007-unified-namespace-conventions.md) (topics, retained state, message names).

> Status: **draft for Wave 6**. Changes before the first release are free; after it, a change is a
> new schema version.

## 1. Topic paths

```
<enterprise>/<site>/<area>/<line>/<cell>/<namespace>/<name>      equipment-level topics
<enterprise>/<site>/<namespace>/<name>                           site-level topics
```

- Segments are the ERP's codes, lower-cased: enterprise = company code, site = plant, then the
  production area, line and work centre (an ISA-95 work cell). Example: `1000/1000/assy/l1/press3`.
- The ERP's equipment tree (`GET /api/production/equipment/`) and the graph's `uns_path` property
  give the same paths. Codes never change once created, so subscriptions never break.
- Topic names use lower case and hyphens. No display names, no spaces.

## 2. Who publishes what

| Topic | Publisher | Retained | ISA-95 object | Purpose |
|---|---|---|---|---|
| `<cell path>/definition/equipment` | ERP | yes | Equipment | What the cell is: name, ISA-95 level, cost centre, activity types |
| `<cell path>/schedule/operation-requests/<order>-<operation>` | ERP | yes | Operations request | One operation of a released order, waiting at this cell |
| `<cell path>/state/status` | MES / edge | yes | Equipment state | What the cell is doing now |
| `<cell path>/events/operation-performance` | MES | no | Operations performance | Work done on an operation: yield, scrap, activity |
| `<enterprise>/<site>/erp/events/confirmation-result` | ERP | no | Operations response | Whether the ERP posted a performance message, and why not |

Rules:

- **Retained = current state.** A new subscriber gets it at once. The publisher clears a retained
  topic by publishing an empty payload, e.g. the ERP clears an operation request when its order is
  completed or cancelled.
- **Every operation is idempotent** ([ADR 0010](decisions/0010-idempotency-of-every-state-changing-operation.md)):
  repeating a message any number of times has the same result as sending it once.
  - Events are not retained and carry an `event_id`, the idempotency key. MQTT QoS 1 can deliver an
    event twice; the consumer processes it once and gives the same answer each time. The ERP stores
    it as the confirmation's `external_id`.
  - Retained state is idempotent by nature: publishing the same state again changes nothing.
  - The same `event_id` with different content is an error (`REJECTED`), never a second posting.
- **QoS 1** for everything. The ERP never subscribes to sensor data: only to `events/operation-performance`.
- **The MES never waits for the ERP.** It keeps working and keeps its events until they are
  acknowledged by a `confirmation-result` (store-and-forward).

## 3. Message envelope

Every payload is UTF-8 JSON with the same envelope:

```json
{
  "schema": "operation-performance/v1",
  "event_id": "press3-2026-06-01T10:15:00Z-0001",
  "timestamp": "2026-06-01T10:15:00+02:00",
  "source": "mes-intelligence",
  "data": { }
}
```

- `schema` names the message type and its version. A consumer ignores schemas it does not know and
  logs them.
- **Numbers that are quantities or money are decimal strings** (`"10.5"`, `"22525.00"`), as in the
  ERP's API ([ADR 0008](decisions/0008-exact-decimals-in-api-responses.md)). Dates are ISO 8601.
- `event_id` is required on events and optional on retained state.

## 4. Messages

### `equipment/v1` (ERP → retained at `<cell path>/definition/equipment`)

```json
"data": {
  "company_code": "1000", "plant": "1000", "area": "ASSY", "line": "L1", "work_center": "PRESS3",
  "name": "Press 3", "isa95_level": "WorkCell", "cost_center": "4000",
  "activity_types": ["LAB", "MACH"], "is_active": true, "uns_path": "1000/1000/assy/l1/press3"
}
```

### `operation-request/v1` (ERP → retained at `<cell path>/schedule/operation-requests/<order>-<op>`)

```json
"data": {
  "order": "MO00000001", "operation_number": 10, "description": "Press frame",
  "material": "SF-3001", "material_description": "Pressed frame", "quantity": "100.000", "unit": "EA",
  "start_date": "2026-06-01", "finish_date": "2026-06-03", "status": "RELEASED",
  "planned_activities": {"LAB": "10.000", "MACH": "10.500"}
}
```

Published when the order is released; cleared (empty retained payload) when it is completed or cancelled.

### `operation-performance/v1` (MES → `<cell path>/events/operation-performance`)

```json
"data": {
  "order": "MO00000001", "operation_number": 10, "posting_date": "2026-06-01",
  "yield_quantity": "100", "scrap_quantity": "0",
  "activities": {"MACH": "10.5", "LAB": "10"}, "final": true
}
```

The ERP posts it with `confirm_operation(..., external_id=event_id)`; the same `event_id` again
returns the confirmation posted the first time. `activities` may be omitted:
the ERP then derives the activity from the plan, as in the API.

### `confirmation-result/v1` (ERP → `<enterprise>/<site>/erp/events/confirmation-result`)

```json
"data": {
  "event_id": "press3-2026-06-01T10:15:00Z-0001", "status": "POSTED",
  "order": "MO00000001", "confirmation_number": "0001", "journal_entry": "PC00000001", "reason": null,
  "replayed": false
}
```

`status` is `POSTED` or `REJECTED` (with the business rule's message in `reason`, e.g. the order is
not released). A repeated event gets **the same result as the first time**: the same `POSTED` with
the same confirmation number and journal entry, plus `"replayed": true` for information. The MES
stops resending once it has a result for the event.

### `equipment-state/v1` (MES or edge → retained at `<cell path>/state/status`)

```json
"data": {"state": "RUNNING", "order": "MO00000001", "operation_number": 10, "since": "2026-06-01T08:00:00+02:00"}
```

`state` is one of `RUNNING`, `IDLE`, `SETUP`, `DOWN`, `OFFLINE`. Informational in version 1: the
ERP does not post anything for it.

## 5. Versioning

- Adding an optional field is not a version change. Removing or renaming a field, or changing its
  meaning, is a new version (`/v2`); publishers send both versions until every consumer has moved.
- This document is versioned with the ERP repository; the MES repository pins the contract version
  it implements.
