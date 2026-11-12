# 0009. The MES lives in its own repository; the Unified Namespace contract is the only link

- **Status:** Accepted, 2026-11-10 (before Wave 6)
- **Implemented:** ERP side in v0.12.0 (Wave 6a); [MES-Intelligence](https://github.com/mokwenaanthony-dev/MES-Intelligence) v0.1.0, 2026-11-12

## Context

Wave 6 connects a shop-floor MES (Rust edge agents, MQTT, a central Python MES) to the ERP through a
Unified Namespace (ADR 0007). The MES has a different runtime, a different release cycle and a
different deployment from the ERP, and it must keep running when the ERP is down.

## Decision

- The MES is a separate repository, **MES-Intelligence**, on the same GitHub account. It does not
  import ERP code and the ERP does not import MES code.
- The two systems share only the **Unified Namespace contract**, kept in the ERP repository as
  [`docs/UNS_CONTRACT.md`](../UNS_CONTRACT.md) because the ERP owns the vocabulary (ADR 0006): topic
  paths, who publishes what, retained or not, and versioned JSON message formats. The MES repository
  links to it and tests against it.
- **Broker: Eclipse Mosquitto** (one container, open source, enough for a UNS of this size). It runs
  in the MES repository's Docker Compose; the ERP connects as a client. A clustered broker (EMQX,
  HiveMQ) can replace it later without changing the contract.
- **First MES release:** the central Python MES and a machine simulator. The Rust edge agent follows
  in the next MES release, publishing to the same topics.
- On the ERP side the work is a **UNS bridge**: a publisher fed by the transactional outbox (ADR 0003)
  and a subscriber that turns operation-performance messages into confirmations through the
  existing service (`orders.confirm_operation`), using the message's event ID as `external_id`.

## Alternatives considered

- **One repository (monorepo):** simpler cross-changes, but it couples releases and CI, mixes Rust
  and Python tooling into the ERP, and blurs the boundary that ISA-95 Levels 3 and 4 are meant to keep.
- **The ERP calls an MES REST API (and back):** tight coupling, and each side would wait for the other.

## Consequences

- A change to a message format is a contract change: a new schema version in `UNS_CONTRACT.md`,
  supported by both sides before the old one is dropped.
- Each repository can be built, tested, released and shown on its own; together they demonstrate
  ISA-95 integration end to end.
- Local development runs the broker from the MES repository's Compose; the ERP points at it with
  `MQTT_URL`. Without `MQTT_URL` the bridge is off, as the graph is off without `NEO4J_URI`.
