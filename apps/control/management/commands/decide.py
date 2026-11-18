"""``manage.py decide --company 1000 --key KEY --accept|--reject [--reason TEXT] --user NAME``.

Records an owner's decision about one recommendation, exactly as ``POST /api/control/decisions/``:
only the owning role (or an administrator) may decide, a rejection needs a reason, and the same
decision again returns the first (idempotent) while a different one is refused.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.common.logcontext import command_request_id
from apps.control import decisions
from apps.organization.models import CompanyCode


class Command(BaseCommand):
    help = "Accept or reject one control-tower recommendation as a user (the owner of the problem)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--key", required=True, help="The problem key shown by manage.py control_tower.")
        choice = parser.add_mutually_exclusive_group(required=True)
        choice.add_argument("--accept", action="store_true")
        choice.add_argument("--reject", action="store_true")
        parser.add_argument("--reason", default="")
        parser.add_argument("--user", required=True, help="Username of the person deciding.")

    def handle(self, *args, **options):
        """Run the command under its own request ID, restored afterwards (no leak into what runs next)."""
        with command_request_id():
            return self._run(*args, **options)

    def _run(self, *args, company, key, accept, reject, reason, user, **options):
        company_code = CompanyCode.objects.filter(code=company).first()
        person = get_user_model().objects.filter(username=user).first()
        if company_code is None or person is None:
            raise CommandError(f"Company code {company} or user {user} does not exist.")
        outcome = "ACCEPTED" if accept else "REJECTED"
        try:
            decision, created = decisions.decide(company_code, key=key, outcome=outcome, reason=reason, user=person,
                                                 on=timezone.localdate())
        except (decisions.DecisionError, decisions.NotOwner) as exc:
            raise CommandError(str(exc)) from exc
        verb = "Recorded" if created else "Already recorded (unchanged)"
        self.stdout.write(self.style.SUCCESS(f"{verb}: {decision.key} {decision.outcome.lower()} by "
                                             f"{decision.decided_by.username}"))
