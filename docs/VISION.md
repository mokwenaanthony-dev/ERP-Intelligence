# Vision: the bottom line this system must achieve

Set by the project owner, Anthony Mokwena. Every wave is measured against it; the scorecard below
says honestly how far the system has come.

## What this project is, and is not

**Not** an attempt to replace SAP. What SAP sells is not code but decades of business rules,
legal localisation for many countries, and a vendor that answers for the result. No developer, and
no AI on its own, replaces that.

**It is** a working, open reference for how an ERP becomes intelligent in the era of AI agents:

- **connected data:** one journal for every module, a knowledge graph of how customers, orders,
  materials, suppliers and machines connect, and a Unified Namespace to the shop floor;
- **predictions with a track record:** forecasts with ranges and their accuracy against what happened;
- **recommendations with reasons and expected impact,** which the owner accepts or rejects on the record;
- **an AI assistant that cites its facts** and never acts on its own.

It follows the direction the large vendors are taking (SAP is building AI agents into its own
product), at a scale one person can read and run. A small or mid-sized company can adopt it **in
steps**: as its own system, or as an intelligence layer beside the ERP it already runs (planned,
Wave 10), because the intelligence sits behind documented services and contracts.

## What it solves

| Problem a business has | What the system does |
|---|---|
| Data trapped in modules and systems, reconciled by hand | One Universal Journal; a knowledge graph of how everything connects |
| Decisions made on last month's reports | KPIs with targets and their basis, on request; a problems inbox with an owner for each problem |
| No view of what is coming | Demand forecasts with ranges; MRP within 15 minutes of a change; revenue at risk per supplier |
| Shop floor and finance out of step | Machine state and confirmations from the MES reach the books and the graph in seconds |
| AI pilots nobody trusts | Recommendations decided by their owner and recorded; an assistant that cites its facts and never executes |
| Supply shocks found too late | Single sources, supplier grades, contract cover and the customer orders that depend on them |
| Rules known only to a few people | A [catalogue of every business rule](RULES.md), generated from the code and kept true by a test |
| Intelligence priced for large companies | Open and modular; every trade-off written down as a decision record |

**Who it is for:**

- owners and managers of small and mid-sized manufacturers and distributors (South Africa first:
  VAT, payroll, B-BBEE and POPIA are built in; other countries' rules are not);
- ERP consultants and partners who want a way to bring AI to clients without replacing their ERP;
- developers learning how enterprise systems and AI fit together.

The architecture, contracts and decisions are written down, so a partner can see exactly what
exists, what is still a gap, and where to join in.

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

- **People decide.** The system proposes; the role that owns the result accepts or rejects, and the
  decision is recorded once (9b). Nothing is sent to a supplier or customer without a person's
  approval. Companies that replaced people and software with AI wholesale have had to walk it back;
  here the AI explains and the owner answers for the result.
- **AI shows its sources.** Every statement of the assistant cites a numbered fact with its source;
  an answer that cites nothing is marked not grounded. It is off until an administrator switches it
  on, because the facts then leave the company.
- **Auditable:** who did what and when; posted documents never change.
- **Predictions show their track record:** ranges, error measures, forecast against actual.
- **Data quality at the door:** validation rules, contract tests between services, problems reported.
- **Privacy and fair competition:** personal data need-to-know (POPIA); across companies only
  anonymised aggregates.
- **Idempotent everywhere:** repeating an operation gives the first result
  ([ADR 0010](decisions/0010-idempotency-of-every-state-changing-operation.md)).

## Scorecard (after Wave 9b)

| Goal | Built | Next |
|---|---|---|
| Everything plugged in | ERP modules on one database; MES and Rust edge agents through the Unified Namespace (MQTT, ISA-95); `ml_service` | Suppliers and customers through a portal and API; an adapter to run beside an existing ERP (Wave 10) |
| Layers integrated in real time | Outbox → knowledge graph and UNS; live machine state; MRP within 15 minutes of a change | Alerts pushed to owners |
| Current state | Live cell state, Customer 360, document flow, dashboards; the control tower: KPIs with targets, status and basis, and a problems inbox with owners (9a) | KPI history over time |
| Future state | Demand forecasts with intervals; MRP projected stock; revenue at risk per supplier; order book and late planned orders beside the KPIs (9a) | — |
| ML predicts | Demand forecasting by backtested method; forecast accuracy against actuals | Supplier delay, machine failure (later) |
| AI recommends | Every problem with an action, a reason and its expected impact; an assistant that explains from cited facts (KG-RAG) and never executes, off by default (9b) | — |
| Stakeholders decide | The owner accepts or rejects, once, with a reason; a track record per kind of recommendation: accepted, rejected, resolved (9b) | Tune the rules from the track record |
| Learned rules as an asset | [269 business rules](RULES.md) catalogued from the code, kept true by a test (9b) | Country rules beyond South Africa in one clear layer |
| Digital supply chain | SRM: onboarding, RFQs, contracts, scorecards | Supplier portal, shared forecast, anonymised benchmarks (Wave 10) |
