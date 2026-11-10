# 0010. Every operation that changes data is idempotent

- **Status:** Accepted, 2026-11-10 (required by the project owner before Wave 6)

## Context

A client that times out cannot know whether its request was processed: the request may have failed,
or it may have posted and only the response was lost. The only safe reaction is to retry, and a
retry must not post a second payment, goods movement or invoice. The same holds for messages: MQTT
with QoS 1 delivers at least once, so the ERP will receive some MES events twice (ADR 0007).

**Requirement:** repeating an operation any number of times produces the same result as doing it
once: the same data in the ERP and the same answer to the caller.

An audit before Wave 6 found:

| Already idempotent | Not idempotent |
|---|---|
| Graph projection and `graph_rebuild` (state-based MERGE) | Every POST that creates a document: journal entries, payments, goods movements, sales and purchase orders, deliveries, invoices, receipts, BOMs, routings, production orders, issues, receipts from orders, confirmations without an ID |
| Reporting view refresh | Status actions (release, confirm, complete, cancel): a repeat returned an error, not the first answer |
| Demo and setup commands (get-or-create) | Confirmations with an `external_id`: a repeat was refused (safe, but a different answer) |
| GET and PUT; payroll (one run per month, enforced by the database) | |

## Decision

**1. API: an `Idempotency-Key` header on every request that changes data** (POST, PUT, PATCH, DELETE
under `/api/`), the pattern used by payment APIs such as Stripe.

- The client generates a unique key per intended operation (a UUID) and sends the same key on every
  retry of that operation. A request without the key is refused with **428 Precondition Required**.
- The first successful (2xx) response is stored with the key, the user, the method, the path and a
  SHA-256 fingerprint of the request body. A repeat with the same key gets **the stored response**,
  byte for byte, with the header `Idempotent-Replayed: true`, and nothing is executed again.
- The same key with a different method, path or body is a client error: **422**, nothing executed.
- A repeat that arrives while the first request is still running gets **409 Conflict** (retry later).
- Failed requests (4xx, 5xx) are not stored: they changed nothing (every posting is one database
  transaction), so a retry with the same key runs again and gives the same failure for the same
  data, or succeeds if the cause was temporary.
- Keys are scoped to the user and kept for 7 days, then purged daily by Celery beat.
- Exempt: `/api/auth/` (signing in issues a new token each time by design; logout is made
  idempotent itself: blacklisting an already blacklisted token succeeds).

**2. Messages: the event ID is the idempotency key.** A confirmation whose `external_id` was already
posted returns the original confirmation (same number, same journal entry). The ERP answers the
MES with the same `confirmation-result` as the first time. The same event ID with different content
is refused.

**3. Background work stays state-based.** Projections and publishers re-read current state and
write it with MERGE or retained messages, so running them twice changes nothing.

**4. Every new endpoint, task, command and message handler must meet this rule.** A test that
repeats the operation is part of its definition of done.

## Alternatives considered

- **Natural keys per document** (e.g. refuse a second invoice with the same supplier reference):
  kept where they exist, but they do not cover every operation and they turn a retry into an error
  instead of the first answer.
- **Optional key:** easier for clients, but one forgotten header re-opens double postings; a
  required key makes the rule visible and testable.
- **Middleware without user scope:** two users could collide on the same key.

## Consequences

- Every client (the test suite, Postman, the MES, future front-ends) sends `Idempotency-Key`.
- One table (`common.IdempotencyRecord`) grows with traffic and is purged after 7 days.
- A retry storm cannot double-post; replays are logged with their request ID.
