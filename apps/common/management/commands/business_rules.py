"""``manage.py business_rules [--check]``: write (or check) ``docs/RULES.md`` from the code.

Without ``--check`` the catalogue is regenerated; with it, the command fails when the file is out of
date (what the test does in CI). See ``apps/common/rules.py``.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.common import rules


class Command(BaseCommand):
    help = "Generate the business-rules catalogue docs/RULES.md from the service layer."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail if docs/RULES.md is out of date.")

    def handle(self, *args, check, **options):
        found = rules.collect()
        text = rules.render(found)
        path = rules.catalogue_path()
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if check:
            if current != text:
                raise CommandError("docs/RULES.md is out of date: run python manage.py business_rules")
            self.stdout.write(self.style.SUCCESS(f"docs/RULES.md is current: {len(found)} rules."))
            return
        if current == text:
            self.stdout.write(f"docs/RULES.md unchanged: {len(found)} rules.")  # idempotent: nothing rewritten
            return
        path.write_text(text, encoding="utf-8", newline="\n")
        self.stdout.write(self.style.SUCCESS(f"docs/RULES.md written: {len(found)} rules."))
