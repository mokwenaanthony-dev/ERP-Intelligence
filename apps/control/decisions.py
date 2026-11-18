"""People decide (Wave 9b, ADR 0018): accepting or rejecting the control tower's recommendations.

The north star: *ML predicts, AI recommends, stakeholders execute or don't*. This module is the last
step: the owner of a problem accepts or rejects its recommendation, says why, and the decision is
kept. Nothing is executed here: accepting records that the owner will act (convert the planned
order, call the supplier ...), in the module where that action lives and with its own checks.

Rules (each one is a sentence a person can check):

* Only the role that owns the problem may decide it (or an administrator). The owner is set by the
  problems inbox: a late customer order belongs to the sales representative, a supply risk to the
  purchasing officer, a cell that is down to the production planner.
* A decision is about a problem that exists now and that the user may see.
* A rejection needs a reason: a rejected proposal is how the rules learn what they get wrong.
* **Idempotent:** one decision per problem. Deciding the same again returns the first decision
  unchanged; a different decision is refused (the record says who decided and when; it is not
  rewritten afterwards).

``acceptance`` turns the decisions into a track record per kind of recommendation: how many were
accepted, and of those, how many problems have since gone away (resolved). A kind that is mostly
rejected, or accepted but never resolved, is a rule to improve. Like the forecasts, the
recommendations show their track record (north star safeguard).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.finance.services import PostingError

from . import services
from .models import Decision

logger = logging.getLogger(__name__)

TENTH = Decimal("0.1")
STATUSES = ("open", "accepted", "rejected")


class DecisionError(PostingError):
    """The decision breaks a rule and was not recorded (the API answers 400 or 409)."""


class DecisionConflict(DecisionError):
    """A different decision was already recorded for the problem (the API answers 409)."""


class NotOwner(PermissionError):
    """The user's role does not own the problem (the API answers 403)."""


def as_dict(decision: Decision) -> dict:
    """A decision as the API shows it."""
    return {"key": decision.key, "kind": decision.kind, "area": decision.area, "title": decision.title,
            "outcome": decision.outcome, "reason": decision.reason, "action": decision.action,
            "kpi": decision.kpi or None, "impact_value": decision.impact_value,
            "decided_by": decision.decided_by.username, "decided_at": decision.decided_at}


def _kind(key: str) -> str:
    return key.split(":", 1)[0]


def may_decide(user, problem: dict) -> bool:
    """True when the user's role owns the problem, or the user is an administrator."""
    return user.is_superuser or user.role == Role.ADMIN or user.role == problem["owner_role"]


def recommendations(company_code, *, on: date, now=None, areas: set[str] | None = None,
                    status: str | None = None) -> list[dict]:
    """The problems inbox with each recommendation's decision (None while open), filtered by status."""
    found = services.problems(company_code, on=on, now=now, areas=areas)
    decided = {d.key: d for d in Decision.objects.filter(company_code=company_code, key__in=[p["key"] for p in found])
               .select_related("decided_by")}
    rows = []
    for problem in found:
        decision = decided.get(problem["key"])
        row = {**problem, "decision": as_dict(decision) if decision else None}
        state = decision.outcome.lower() if decision else "open"
        if status is None or status == state:
            rows.append(row)
    return rows


def _repeat_or_refuse(existing: Decision, outcome: str) -> Decision:
    """Idempotency: the same decision again is the first one; a different one is refused."""
    if existing.outcome == outcome:
        logger.info("Decision on %s repeated: returning the first (%s)", existing.key, existing.outcome)
        return existing
    raise DecisionConflict(
        f"{existing.key} was already {existing.outcome.lower()} by {existing.decided_by.username} on "
        f"{timezone.localtime(existing.decided_at):%Y-%m-%d %H:%M}; a decision is not changed afterwards.")


def decide(company_code, *, key: str, outcome: str, reason: str = "", user, on: date,
           now=None) -> tuple[Decision, bool]:
    """Record the owner's decision about one recommendation; returns (decision, created).

    Raises ``DecisionError`` for a broken rule, ``DecisionConflict`` when a different decision exists,
    and ``NotOwner`` when the user's role does not own the problem.
    """
    if outcome not in Decision.Outcome.values:
        raise DecisionError(f"Outcome must be {' or '.join(Decision.Outcome.values)}.")
    reason = (reason or "").strip()
    existing = Decision.objects.filter(company_code=company_code, key=key).select_related("decided_by").first()
    if existing:
        return _repeat_or_refuse(existing, outcome), False
    if outcome == Decision.Outcome.REJECTED and not reason:
        raise DecisionError("A rejection needs a reason: it is how the recommendations improve.")
    problem = next((p for p in services.problems(company_code, on=on, now=now, areas=services.visible_areas(user))
                    if p["key"] == key), None)
    if problem is None:
        raise DecisionError(f"There is no current problem {key} that you may see (it may already be solved).")
    if not may_decide(user, problem):
        raise NotOwner(f"{problem['title']} is owned by the {problem['owner']} role.")
    advice = problem["recommendation"]
    try:
        with transaction.atomic():
            decision = Decision.objects.create(
                company_code=company_code, key=key, kind=_kind(key), area=problem["area"], title=problem["title"],
                action=advice["action"], kpi=advice["impact"]["kpi"] or "", impact_value=advice["impact"]["value"],
                outcome=outcome, reason=reason, decided_by=user, decided_at=now or timezone.now())
    except IntegrityError:
        # Someone decided the same problem at the same moment: theirs is the first decision.
        existing = Decision.objects.select_related("decided_by").get(company_code=company_code, key=key)
        return _repeat_or_refuse(existing, outcome), False
    logger.info("Decision recorded on %s: %s by user %s", key, outcome, user.pk)
    return decision, True


def _rate(part: int, whole: int) -> Decimal | None:
    return (Decimal(part) * 100 / whole).quantize(TENTH) if whole else None


def acceptance(company_code, *, on: date, now=None, days: int = 90) -> dict:
    """The track record of the recommendations: decided, accepted, rejected and resolved, per kind.

    *Resolved* means an accepted problem is no longer in the inbox today. The latest rejection
    reasons are listed per kind: they say what the rule gets wrong.
    """
    since = (now or timezone.now()) - timedelta(days=days)
    current = services.problems(company_code, on=on, now=now)
    open_keys = {p["key"] for p in current}
    decided_keys = set(Decision.objects.filter(company_code=company_code, key__in=open_keys)
                       .values_list("key", flat=True))
    kinds: dict[str, dict] = {}
    for decision in Decision.objects.filter(company_code=company_code, decided_at__gte=since).order_by("-decided_at"):
        row = kinds.setdefault(decision.kind, {"kind": decision.kind, "area": decision.area, "decided": 0,
                                               "accepted": 0, "rejected": 0, "accepted_resolved": 0,
                                               "latest_rejection_reasons": []})
        row["decided"] += 1
        if decision.outcome == Decision.Outcome.ACCEPTED:
            row["accepted"] += 1
            row["accepted_resolved"] += decision.key not in open_keys
        else:
            row["rejected"] += 1
            if len(row["latest_rejection_reasons"]) < 3:
                row["latest_rejection_reasons"].append(decision.reason)
    rows = sorted(kinds.values(), key=lambda r: r["kind"])
    for row in rows:
        row["acceptance_rate"] = _rate(row["accepted"], row["decided"])
        row["resolved_rate"] = _rate(row["accepted_resolved"], row["accepted"])
    decided = sum(r["decided"] for r in rows)
    accepted = sum(r["accepted"] for r in rows)
    return {"company_code": company_code.code, "days": days, "decided": decided, "accepted": accepted,
            "rejected": decided - accepted, "acceptance_rate": _rate(accepted, decided),
            "open_recommendations": len(open_keys - decided_keys), "by_kind": rows}
