# 0005. Troubleshooting logs with request IDs and error locations

- **Status:** Accepted
- **Wave:** 4c (release v0.8.1)

## Context

With Celery workers, a graph projector and soon an MES sending events, one problem can leave traces
in several processes. A log line must say where it came from and which request or task it belongs
to, without exposing personal or pay data.

## Decision

- One format everywhere: time, level, request ID, logger, `module.function:line`, message.
- A request ID per API call, returned in the `X-Request-ID` header; a caller's own ID is reused only
  if it matches a strict pattern, so it cannot forge log lines. Celery tasks use `task:<id>`.
- One DRF exception handler: rejected business rules are WARNINGs with the file and line where the
  rule was raised; other client errors are INFO; unexpected errors are ERRORs with a traceback in
  `logs/errors.log`, and the caller gets a 500 with only the request ID.
- Services log business events explicitly (document numbers, counts), never names, salaries,
  passwords, tokens, query strings or request bodies.
- Django's own `django.request` logger is raised to ERROR (Wave 5), because the API already logs
  every 4xx once, with its request ID.

## Alternatives considered

- **A logging decorator on every service:** less code, but the logged function and line would be
  the decorator's, not the real code's.
- **Central logging (ELK, Loki) now:** still planned; the structured format and request IDs make
  it straightforward to add.

## Consequences

- `grep <request id> logs/erp.log` shows everything one call did.
- The MES can send its own request IDs in Wave 6, so a confirmation can be traced from the shop
  floor to the journal entry.
