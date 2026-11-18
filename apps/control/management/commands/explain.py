"""``manage.py explain --company 1000 --key KEY --user NAME [--question TEXT]``.

Shows the numbered, sourced facts about one problem and, when the AI assistant is on (an
administrator switched it on and a provider key is set), the model's explanation with the facts it
cites. Exactly as ``POST /api/control/explain/``; nothing is executed.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.common.logcontext import command_request_id
from apps.control import assistant
from apps.organization.models import CompanyCode


class Command(BaseCommand):
    help = "Explain one control-tower problem from grounded facts (and the AI assistant, if it is on)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--key", required=True)
        parser.add_argument("--user", required=True, help="Whose view: facts are limited to what this user sees.")
        parser.add_argument("--question", default="")

    def handle(self, *args, **options):
        """Run the command under its own request ID, restored afterwards (no leak into what runs next)."""
        with command_request_id():
            return self._run(*args, **options)

    def _run(self, *args, company, key, user, question, **options):
        company_code = CompanyCode.objects.select_related("currency").filter(code=company).first()
        person = get_user_model().objects.filter(username=user).first()
        if company_code is None or person is None:
            raise CommandError(f"Company code {company} or user {user} does not exist.")
        try:
            result = assistant.explain(company_code, key=key, user=person, on=timezone.localdate(), question=question)
        except assistant.AssistantError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.MIGRATE_HEADING(result["title"]))
        for fact in result["facts"]:
            self.stdout.write(f"  [{fact['id']}] {fact['text']}")
            self.stdout.write(f"        source: {fact['source']}")
        self.stdout.write(f"  Graph: {result['graph']}")
        self.stdout.write(self.style.MIGRATE_HEADING(f"AI assistant: {result['assistant']['why']}"))
        if result["assistant"].get("error"):
            self.stdout.write(self.style.WARNING(f"  {result['assistant']['error']}"))
        if result["answer"]:
            self.stdout.write(f"  {result['answer']['text']}")
            cited = ", ".join(s["id"] for s in result["answer"]["sources"]) or "none (not grounded)"
            self.stdout.write(f"  Cites: {cited}. {result['answer']['note']}")
