"""``manage.py uns_contract [--check] [--message FILE]``: the UNS contract's schema files (ADR 0021).

* No options: check every example in ``contract/uns/v1/examples/`` against its schema and write
  ``contract/uns/v1/SHA256SUMS``, the checksums the MES pins. Run it after changing a schema.
* ``--check``: the same checks, but fail instead of writing when ``SHA256SUMS`` is out of date (CI).
* ``--message FILE``: check one payload, e.g. one captured with
  ``mosquitto_sub -t '<topic>' -C 1 > f.json``, and print every way it breaks the contract.

Reads files only; never connects to the broker and changes nothing in the database, so it is safe to
run any number of times.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.uns import schemas


class Command(BaseCommand):
    help = "Check the UNS contract's examples (or one message) against its JSON Schemas; keep SHA256SUMS."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail if SHA256SUMS is out of date.")
        parser.add_argument("--message", help="A JSON payload file to check against the schema it names.")

    def handle(self, *args, check, message, **options):
        if message:
            path = Path(message)
            if not path.is_file():
                raise CommandError(f"No such file: {path}")
            problems = schemas.errors(path.read_bytes())
            if problems:
                raise CommandError("Does not follow the contract:\n  " + "\n  ".join(problems))
            kind = json.loads(path.read_bytes() or b"{}").get("schema", "an empty payload (clears a retained topic)")
            self.stdout.write(self.style.SUCCESS(f"{path.name} follows the contract ({kind})."))
            return

        failed = []
        examples = sorted(schemas.EXAMPLES_DIR.glob("*.json"))
        for path in examples:
            problems = schemas.errors(path.read_bytes())
            failed += [f"{path.name}: {p}" for p in problems]
        if failed:
            raise CommandError("Examples that break the contract:\n  " + "\n  ".join(failed))
        self.stdout.write(f"{len(examples)} example(s) follow {len(schemas.message_types())} message schemas.")

        expected = schemas.checksums()
        current = schemas.SUMS_FILE.read_text(encoding="utf-8") if schemas.SUMS_FILE.exists() else ""
        if current.replace("\r\n", "\n") == expected:
            self.stdout.write(self.style.SUCCESS("SHA256SUMS is up to date."))
        elif check:
            raise CommandError("contract/uns/v1/SHA256SUMS is out of date: run `python manage.py uns_contract` "
                               "and commit it (the MES pins these checksums).")
        else:
            schemas.SUMS_FILE.write_text(expected, encoding="utf-8", newline="\n")
            self.stdout.write(self.style.SUCCESS("SHA256SUMS written."))
