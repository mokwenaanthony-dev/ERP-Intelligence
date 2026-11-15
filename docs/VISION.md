# Vision: the bottom line this system must achieve

Set by the project owner, Anthony Mokwena. Every wave is measured against it; the scorecard below
says honestly how far the system has come.

## The goal

- Everything and everyone is plugged into the network.
- The layers of the business are integrated and operate on data and information from all the other
  layers, in real time.
- Stakeholders know the **state** of the business in real time, and its **future state**.
- ML predicts future outcomes from past patterns and the current state; AI recommends operational
  adjustments to improve them; **stakeholders execute, or do not execute**, the recommendations.
- **Data as the primary commodity:** connect, collect, store, analyse, visualise, find patterns,
  report problems, solve.
- **A digital supply chain:** connected to all potential suppliers and buyers, learning from each other.

## What "real time" means here

| Layer | Target | How |
|---|---|---|
| Machine state | under 5 seconds | Edge agent → Unified Namespace (retained state) → ERP and graph |
| Business events on the graph | under 15 seconds | Transactional outbox, projected every 10 s |
| Planning impact of a change | under 15 minutes | MRP re-checked every 15 minutes; unchanged data returns the stored run |
| Demand forecast | monthly, and on request | `ml_service`, a forecast run on the 1st |
| Financial view | daily | Reporting views refreshed by Celery beat |

## The business results it should move

On time in full (to customers, from suppliers) · forecast accuracy · inventory days · revenue at risk.

## Safeguards

- **People decide.** The system proposes; the role that owns the result executes or rejects. Nothing
  is sent to a supplier or customer without a person's approval.
- **Auditable:** who did what and when; posted documents never change.
- **Predictions show their track record:** ranges, error measures, forecast against actual.
- **Data quality at the door:** validation rules, contract tests between services, problems reported.
- **Privacy and fair competition:** personal data need-to-know (POPIA); across companies only
  anonymised aggregates.
- **Idempotent everywhere:** repeating an operation gives the first result
  ([ADR 0010](decisions/0010-idempotency-of-every-state-changing-operation.md)).

## Scorecard (after Wave 8c)

| Goal | Built | Next |
|---|---|---|
| Everything plugged in | ERP modules on one database; MES and Rust edge agents through the Unified Namespace (MQTT, ISA-95); `ml_service` | Suppliers and customers through a portal and API (Wave 10) |
| Layers integrated in real time | Outbox → knowledge graph and UNS; live machine state; MRP within 15 minutes of a change | — |
| Current state | Live cell state, Customer 360, document flow, dashboards | One live control-tower view and a problems inbox (Wave 9) |
| Future state | Demand forecasts with intervals; MRP projected stock; revenue at risk per supplier | KPIs side by side, today and projected (Wave 9) |
| ML predicts | Demand forecasting by backtested method; forecast accuracy against actuals | Supplier delay, machine failure (later) |
| AI recommends | MRP planned orders and messages; supplier-risk recommendations; KG-RAG endpoint | Recommendations with expected impact; an AI agent that recommends, never executes (Wave 9) |
| Stakeholders decide | Planned orders converted by the owning role | Decisions recorded and learned from (Wave 9) |
| Digital supply chain | SRM: onboarding, RFQs, contracts, scorecards | Supplier portal, shared forecast, anonymised benchmarks (Wave 10) |
