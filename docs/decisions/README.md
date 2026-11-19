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
| [0009](0009-mes-in-its-own-repository.md) | The MES lives in its own repository; the UNS contract is the only link | Accepted | 6 |
| [0010](0010-idempotency-of-every-state-changing-operation.md) | Every operation that changes data is idempotent (`Idempotency-Key`, event IDs) | Accepted | 5c |
| [0011](0011-live-cell-state-in-the-erp-and-the-graph.md) | Live cell state: the latest state in the ERP, projected onto the graph | Accepted | 6d |
| [0012](0012-crm-in-the-monolith-on-the-business-partner.md) | CRM in the monolith, on the Business Partner; quotations become sales orders through the sales services | Accepted | 7a |
| [0013](0013-srm-on-the-business-partner-and-the-purchasing-services.md) | SRM in the monolith: onboarding creates the Business Partner; awards and releases are purchase orders through the purchasing services | Accepted | 7b |
| [0014](0014-demand-forecasting-in-a-stateless-ml-service.md) | Demand forecasting in a stateless FastAPI `ml_service`; the ERP keeps histories and forecasts | Accepted | 8a |
| [0015](0015-mrp-on-dated-requirements-with-planned-orders.md) | MRP nets dated requirements into planned orders; people convert them through the module services | Accepted | 8b |
| [0016](0016-supply-risk-and-forecast-accuracy-in-the-service-layer.md) | Revenue at risk and forecast accuracy are computed in the service layer; MRP re-checked every 15 minutes | Accepted | 8c |
| [0017](0017-control-tower-kpis-and-problems-inbox.md) | The control tower calculates KPIs and gathers problems on request, from the modules that already know them | Accepted | 9a |
| [0018](0018-recommendations-decided-by-owners-and-a-grounded-ai-assistant.md) | Owners decide recommendations once; the AI assistant explains from cited facts and never executes | Accepted | 9b |
| [0019](0019-a-partner-portal-in-the-monolith-behind-a-role-wall.md) | A partner portal in the monolith, behind a role wall; partners inform, the company decides | Accepted | 10a |
| [0020](0020-side-by-side-imports-through-the-same-rules.md) | Side by side: another ERP's exports load through the same rules, idempotently, row by row | Accepted | 10b |
