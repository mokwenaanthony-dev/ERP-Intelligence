# 0021. The UNS contract as JSON Schema files, checked in CI on both sides

- **Status:** Accepted, 2026-11-23 (Wave 6e)

## Context

The ERP and the MES share one thing: the Unified Namespace contract, version 1
([`docs/UNS_CONTRACT.md`](../UNS_CONTRACT.md), [ADR 0009](0009-mes-in-its-own-repository.md)). It is
versioned in Git and both repositories build against it, but it is written for people. Each side
codes the messages by hand from the document. The ERP's tests check the ERP against the ERP's own
idea of the contract, and the MES's tests do the same on the other side. Nothing fails when the two
ideas drift apart: a field renamed, a quantity sent as a JSON number instead of a decimal string, a
date without its offset. The drift would show up on the shop floor, as rejected confirmations.

The industry argument behind this has a name, "UNS as code" (Carsten Dyhr-Nielsen, November 2026).
The platforms that offer to give AI agents context (Databricks, Microsoft Fabric, AVEVA) each want to
own the semantic model of the plant. If each one builds its own, the company "rents back its own
factory, once per vendor". The model should be declared in files the company owns, in Git, and every
platform should read it, not define it. An agent working against a versioned contract reads it; when
it wants to change something, it opens a pull request.

## Decision

- **The contract has a machine-readable form:** `contract/uns/v1/`, one JSON Schema (draft
  2020-12) file per message type (`equipment`, `operation-request`, `operation-performance`,
  `confirmation-result`, `equipment-state`). Each refines a shared `envelope.schema.json`, which also
  holds the shared types: exact decimal strings (ADR 0008), ISO 8601 dates and date-times with an
  offset, ERP codes, operation numbers, and the rule that an event needs its `event_id` (ADR 0010).
  The document stays the explanation; the files are what is checked.
- **Examples are part of the contract:** `contract/uns/v1/examples/` has one complete message per
  type (and a rejected answer). They must follow their schemas, and the other side can use them as
  test data.
- **The ERP is checked in CI** (`tests/test_uns_contract.py`). Everything it publishes follows its
  schema: equipment, operation requests (with and without event IDs, and cleared), and every answer
  it gives the MES (posted, replayed, rejected for each kind of reason). It sends no field the
  schema does not document. The messages its tests send as the MES must follow the contract too, so
  the ERP is tested with what the MES really sends.
- **The MES pins the files by checksum.** `contract/uns/v1/SHA256SUMS` lists every file's SHA-256
  (line endings normalised, so a Windows checkout gives the same sums). A test fails when it is out
  of date (`python manage.py uns_contract` rewrites it). The MES repository keeps a copy of the
  folder and checks the copy against the sums of the release it implements. A contract change is
  then always a visible, reviewed change on both sides, never a silent one.
- **Publishers are strict, consumers stay tolerant.** The schemas say what every publisher must
  send. The ERP's listener is not made stricter by them: it still posts a quantity sent as a JSON
  number and answers anything it cannot post with `REJECTED` and a reason. A schema change can
  never stop production; it fails a build instead.
- **Versioning is unchanged** (contract section 5). Adding an optional field is not a new version, so
  the schemas allow fields they do not list. The ERP's own test is stricter: it sends only
  documented fields, so an undocumented field cannot slip into the contract by accident.
- **A tool for people:** `python manage.py uns_contract --message f.json` checks one payload captured
  from the broker and lists every way it breaks the contract. Without options the command checks
  the examples and keeps `SHA256SUMS`; `--check` fails instead of writing (for CI). It reads files
  only.

## What the schemas found

Writing them exposed one gap in the document. The ERP answers every performance message, even one
that is not readable JSON or has no `event_id`; such an answer has `"event_id": null`, which the
document did not allow ("`event_id` is required on events"). The behaviour is right (the answer is
logged and costs nothing), so the contract now says it: the event ID is null only when the message
it answers had none, and such a message cannot be retried. A clarification of existing behaviour,
not a version change.

## Alternatives considered

- **Pydantic models shared as a package:** what the `ml_service` contract uses (ADR 0014), inside one
  language. The MES has a Rust edge agent, and a broker, a historian or an AI platform may read the
  same topics. JSON Schema is language-neutral and read by most of them.
- **A schema registry (Confluent, Azure Schema Registry):** the standard for Kafka at enterprise
  scale. It is one more service to run, and it moves the model out of Git, where people review
  changes. A registry can be fed from these files later.
- **AsyncAPI:** describes topics, brokers and messages together, with JSON Schema for the payloads.
  A good next step for documentation; the payload schemas are the part that catches drift, so they
  come first.
- **Validating every incoming message in the listener:** stricter, but a schema mistake would then
  reject real work on the shop floor. A tolerant reader with strict publishers fails builds, not
  production.

## Consequences

- Drift between the ERP and the MES fails the build of whichever side caused it, not a shift on the
  shop floor.
- The contract is something an AI agent, a historian or a new consumer can read and check against
  without asking anyone, and that a change proposes in a pull request.
- `jsonschema` (and `referencing`) are new dependencies. The schemas reference each other by `$id`,
  resolved from the files on disk: nothing is ever fetched from the network.
- Not yet: the MES side of the check (MES-Intelligence pins the files and validates what its Python
  MES and Rust edge agent publish; a following MES release), an AsyncAPI document, and the "source"
  tier of a UNS model (which OPC UA or Modbus address feeds which signal at each site), which
  belongs at the edge with the MES.
