# 0011. Live cell state: the latest state in the ERP, projected onto the graph

- **Status:** Accepted, 2026-11-12 (Wave 6d); amended 2026-11-23 in v0.23.1 (a last will is accepted)

## Context

The Unified Namespace and the knowledge graph depend on each other (ADR 0006): the graph gives
UNS topics their meaning, and the UNS should keep the graph's digital twin live. Since MES 0.2.0,
edge agents publish each cell's state (`equipment-state/v1`, retained at `<cell>/state/status`):
RUNNING with the order being worked on, IDLE, DOWN or OFFLINE (the agent's last will). Planners
want the question "which cells are down, and which orders does that hold up?" answered from the
same graph that already knows orders, materials and customers.

The graph is a read model, rebuilt from the ERP's tables (ADR 0003), so writing UNS messages
straight into Neo4j would break `graph_rebuild` and put a second writer on the graph.

## Decision

- **The ERP keeps the latest state per cell** in `production.CellState` (one row per work centre),
  written by the UNS listener (`uns_listen` now also subscribes to `+/+/+/+/+/state/status`).
  Only the latest: a history of machine states belongs in a time-series store, not in the system of
  record ("the UNS is not a database", ADR 0007).
- **Idempotent and order-safe:** an unchanged state is not written (the retained message arrives on
  every reconnect); a report older than the stored one (by the message `timestamp`) is ignored; an
  empty retained payload deletes the row (state unknown). Unknown cells, states and schemas are
  logged and skipped. The ERP sends no answer: state is not an event.
- **The outbox projects it** (event type `production.cellstate`) onto the `Cell` node: `state`,
  `state_since`, `state_order`, `state_operation`, `state_reported_at`, and a `RUNNING` relationship
  to the production order being worked on, only if the ERP has that order. Ontology 1.3.
- **Where it shows:** the equipment tree (`GET /api/production/equipment/`, `live_state` per cell),
  the graph query `GET /api/graph/shop-floor/?plant=` (cells DOWN or OFFLINE, the released
  production orders with operations there, the open sales orders for what they make, each filtered
  by role), and KG-RAG context (the cell's properties).

**v0.23.1: the last will.** "Older never overwrites newer" had a hole, found by reading Volkan
Alkılıç's post on frozen values ("The device died. Your dashboard still says 72.4 °C"). MES 0.2.0's
edge agent registers its OFFLINE last will when it connects, so when it dies the broker publishes
that message stamped with the connection's time, hours older than the agent's last report. The ERP
ignored it as older and kept the cell RUNNING in the equipment tree and on the graph, and the
control tower raised nothing. The broker's retained topic said OFFLINE; only the ERP's copy was
frozen. Now:

- An OFFLINE from the **same source** as the stored state is accepted whatever its timestamp: a
  will cannot know when it will be sent. `since` becomes the time the ERP heard it, the order is
  cleared, and `reported_at` stays the newest seen, so a delayed report from before the failure is
  still ignored and time never goes backwards. The same will again changes nothing.
- An OFFLINE from another source still follows the timestamp rule: one publisher's will cannot
  overrule another's report.
- An optional `session` in `equipment-state/v1` (like Sparkplug's bdSeq) tells the will of an
  earlier connection from the current one: if both carry a session and they differ, the will is
  ignored. Until the MES sends it (MES 0.2.1), a will that the broker publishes after the agent has
  already reconnected could mark a running cell OFFLINE until its next report. The broker's
  retained topic has the same problem (it keeps the late will too), so the real fix is on the edge.
- Not yet: freshness. With report by exception, a steady cell sends nothing, so the ERP cannot tell
  a quiet cell from a silent one. That needs a heartbeat from the edge first (MES 0.2.1); then the
  ERP can mark a state stale when it has not been confirmed in time.

## Alternatives considered

- **Listener writes Neo4j directly:** fewer steps and fresher, but the graph would no longer be
  rebuildable from the ERP, and a Neo4j outage would lose states instead of queueing them.
- **Keep the history of states in the ERP:** useful for OEE, but high-volume telemetry in the system
  of record; it belongs in a time-series store fed from the UNS (a later wave).
- **Ask the MES for states over an API:** couples the ERP to the MES (ADR 0009).

## Consequences

- The graph's twin lags the shop floor by the outbox cycle (about 10 s); fine for planning, not for
  machine control, which never goes through the ERP anyway.
- A cell that has never reported shows no state: the ERP says "unknown" rather than guessing IDLE.
- OEE, downtime reasons and state history need a time-series store; this table does not grow.
