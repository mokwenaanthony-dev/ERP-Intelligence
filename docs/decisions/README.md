# Architecture decision records

Each record explains one significant decision: the situation, what was decided, what else was
considered and what it costs. Records are never rewritten to change history. When a decision
changes, a new record **supersedes** the old one and both stay, so the reasoning at each point in
time can always be read back.

Format: one file per decision, `NNNN-short-title.md`, with Status, Context, Decision, Alternatives
and Consequences. Smaller day-to-day choices are listed per wave in the project roadmap
("Wave N decisions").

| No. | Decision | Status | Wave |
|---|---|---|---|
| [0001](0001-modular-monolith-and-service-layer.md) | Modular monolith with business rules in a service layer | Accepted | 0 |
| [0002](0002-universal-journal-and-immutable-documents.md) | One Universal Journal; posted documents are immutable | Accepted | 1 |
| [0003](0003-read-models-fed-by-a-transactional-outbox.md) | Analytics and graph are read models fed by a transactional outbox | Accepted | 4a, 4b |
| [0004](0004-neo4j-property-graph-and-kg-rag.md) | Neo4j property graph with an explicit ontology; KG-RAG, not GraphRAG | Accepted | 4b, revised 5 |
| [0005](0005-troubleshooting-logs-and-request-ids.md) | Troubleshooting logs with request IDs and error locations | Accepted | 4c |
| [0006](0006-isa-95-equipment-hierarchy-as-shared-vocabulary.md) | ISA-95 equipment hierarchy, owned by the ERP, shared by the graph and the UNS | Accepted | 5 |
| [0007](0007-unified-namespace-conventions.md) | Unified Namespace conventions and ISA-95 message names | Accepted (built in Wave 6) | 5, 6 |
| [0008](0008-exact-decimals-in-api-responses.md) | Money and quantities in API responses are exact decimal strings | Accepted | 5b |
