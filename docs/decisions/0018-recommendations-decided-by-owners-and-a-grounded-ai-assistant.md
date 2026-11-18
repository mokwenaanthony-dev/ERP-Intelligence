# 0018. Owners decide recommendations once; the AI assistant explains from cited facts and never executes

- **Status:** Accepted, 2026-11-18 (Wave 9b); amended in v0.20.1 (MRP recommendations name the planned order)

## Context

The north star ([`docs/VISION.md`](../VISION.md)) ends with *AI recommends, stakeholders execute or
don't*. After Wave 9a the control tower showed the problems and who owns them, but not what to do
about each one, and nothing recorded what people decided, so there was no way to learn which
recommendations were any good.

The market shows why the human step matters. Klarna announced it would replace its software vendors
and customer-service staff with AI, then went back to other software and hired people again when
quality dropped (as told in Noel D'Costa's "Can Claude replace SAP?", 2026). An ERP is trusted
because someone answers for the result. And language models can be "confidently, politely wrong", so
an AI explanation has to show what it is based on.

## Decision

- **Every problem carries a recommendation**: an action, the reason, and the expected impact (the
  KPI it should move, and the money involved where there is any: open sales value, overdue purchase
  value, revenue at risk). These come from simple rules in `apps/control/services.py` that use what
  the modules already know (stock on hand for a late order, other cells on the line for a cell that
  is down, the supplier-risk advice of 8c).
- **Only the owner decides**: the role the inbox names as owner, or an administrator. A decision is
  `ACCEPTED` or `REJECTED`, and a rejection needs a reason, because rejected proposals are how the
  rules get better.
- **One decision per problem, and it is idempotent.** There is a unique constraint on company code
  and problem key. Sending the same decision again returns the first one (200). Sending a different
  one is refused (409) with who decided and when. The admin screen is read-only. A cell's key now
  includes when it failed, so a second breakdown is a new problem. The recommendation and its impact
  are copied into the decision, so the record still says what was proposed after the problem is gone.
- **Accepting is not executing.** The owner then acts in the module that owns the action (convert
  the planned order, deliver, raise the RFQ), with that module's own checks.
- **A track record per kind of recommendation**: how many were decided, accepted and rejected, how
  many accepted problems have since gone from the inbox (*resolved*), and the latest rejection
  reasons. A kind that is mostly rejected, or accepted but never resolved, is a rule to improve.
- **The AI assistant explains from numbered facts.** These are:
  - the problem, the recommendation, the expected impact, the KPI with its basis, and the track
    record (F1 to F5);
  - the problem's neighbourhood in the knowledge graph (G1 and on), limited to the labels the
    user's role may see (KG-RAG, [ADR 0004](0004-neo4j-property-graph-and-kg-rag.md)).

  The model must cite facts as `[F2]`. The cited ids are checked against the facts given, ids that
  are not among them are dropped, and an answer that cites nothing is marked `grounded: false`.
- **The assistant is off by default and has no tools.** It runs only when an administrator switches
  it on (`AssistantSetting`) *and* a provider key is set in the environment, because the facts are
  then sent outside the company. The key is never stored in the database, shown or logged. The
  prompt tells the model it cannot act and that text inside the facts is data, not instructions. If
  the provider is unreachable, the facts are still returned.
- **Answers are stored by fingerprint**: a SHA-256 of the model, the question and the facts. Asking
  again about unchanged facts returns the stored answer without calling the model. This is the same
  fingerprint idea as the MRP and forecast runs.
- **The business rules become a catalogue.** `docs/RULES.md` lists every business-rule error the
  service layer raises, with its message, module and function. It is generated from the code with
  `ast` (`python manage.py business_rules`), and a test fails when the file and the code disagree.
  The rules are the part of an ERP worth more than its code, so they are written down where people
  can read them.

**v0.20.1.** The first MRP recommendations only repeated the MRP message ("act on the MRP
message"), which the Docker output showed was no help. An MRP message now points at the late
planned order it is about (`MRPMessage.planned_order`; runs stored earlier are linked by the
migration). The recommendation names that order (`MR00000004/3`), what converting makes of it, and
the conversion endpoint. A converted order points at its document instead. A BUY order is owned by
purchasing, a MAKE order by the planner, the same split as the conversion itself (ADR 0015).

## Alternatives considered

- **Letting the AI act (an agent with tools that converts orders or sends emails):** this is exactly
  what the safeguards forbid. The responsibility would sit with no one.
- **A free-text decision note without a fixed outcome:** it cannot be counted, so nothing could be
  learned from it.
- **Changeable decisions:** convenient, but the record would no longer show what was decided at the
  time. A new situation is a new problem with a new key.
- **The AI on by default with a key in the settings:** sending company data to an outside model is a
  choice an administrator makes on purpose (POPIA, confidentiality).
- **Retrieval by vector search over documents:** the graph already holds the validated relationships
  with their keys, so facts can be cited back to records. Vector search may be added later for free
  text such as ticket notes.
- **A hand-written rules document:** it would go stale within a wave. Generating it keeps it true.

## Consequences

- The loop of the vision is closed: the ML predicts, the rules recommend with reasons and impact, and
  the owner decides on the record. The record then shows which recommendations work.
- Every AI statement can be checked against a numbered fact with a source. There is no hidden
  reasoning and no action.
- The assistant costs nothing and sends nothing until an administrator chooses to switch it on.
- 269 rules are visible in one document. A new rule shows up in the same pull request that adds it.
- Later, rejection reasons and resolved rates could tune severities and thresholds. The rules stay
  readable, and any change to them is a reviewed pull request, not hidden learning.
