# 0007. Unified Namespace conventions and ISA-95 message names

- **Status:** Accepted 2026-11-10; built in Wave 6

## Context

Wave 6 connects a separate MES (Rust edge agents, MQTT, Python MES) to the ERP through a Unified
Namespace. Deciding the conventions now keeps Wave 5's data model compatible with them.

## Decision

- **Topic path:** `<enterprise>/<site>/<area>/<line>/<cell>/<namespace>/<name>`, using the ISA-95
  hierarchy of ADR 0006.
- **State topics are retained; event topics are not.** Current state (`.../state/status`,
  `.../production-order/current`) is retained and published only when it changes (report by
  exception), so a new subscriber gets it at once. Events (`.../events/operation-confirmed`) are not
  retained and carry an event ID, so a consumer that receives one twice processes it once.
- **Plain MQTT with JSON for the business layer; Sparkplug B only at the edge.** Sparkplug B fixes
  its own topic shape (group / edge node / device), which does not follow ISA-95 paths. Edge agents
  may use it; the business layer uses the paths above, joined by the edge agent or a bridge.
- **The UNS is not a database.** It holds latest state and passes events on. History stays in the
  ERP (business documents) and a time-series store (sensor readings).
- **ISA-95 message names (Parts 4 and 5):** ERP → MES sends the operations schedule / operations
  request (production orders) and operations, material and equipment definitions (routings, BOMs,
  material master, the hierarchy). MES → ERP sends operations performance / operations response
  (confirmations, consumption, production, scrap, machine and labour hours). Payloads follow B2MML
  where that helps.
- **The ERP publishes from its transactional outbox** (ADR 0003), the same one that feeds the graph.
- **Levels:** the ERP is ISA-95 Level 4 (master data, plans, costs); the MES is Level 3 (execution).
  Neither writes into the other's database. The MES runs on when the ERP is down and forwards its
  events later (store-and-forward).

## Alternatives considered

- **ERP calls the MES API directly (and back):** tight coupling, and the shop floor would wait for
  the ERP.
- **Sparkplug B for everything:** standard device lifecycle and compression, but its topic shape
  hides the ISA-95 structure that makes the namespace readable.

## Consequences

- Anyone who knows ISA-95 recognises the interface without reading our code.
- The ERP subscribes only to the business events it posts; sensor traffic never reaches it.
- Duplicate deliveries, normal with MQTT QoS 1, are safe because events carry IDs.
