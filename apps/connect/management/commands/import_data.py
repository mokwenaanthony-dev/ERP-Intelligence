"""``manage.py import_data --company 1000 --source generic --object partners --file partners.csv [--user NAME]``.

Loads one file exported from another ERP, exactly as ``POST /api/connect/imports/``: through the
modules' own rules, one row at a time, idempotent (the same file again returns the first batch;
rows already loaded stay unchanged; rows rejected before are tried again). Prints the counts and
every rejected row with its reason.
Load the objects in this order: partners, materials, stock, purchase_orders, sales_orders.
"""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.common.logcontext import command_request_id
from apps.connect import services
from apps.connect.mappings import MAPPINGS, FileError
from apps.connect.models import ObjectType
from apps.organization.models import CompanyCode


class Command(BaseCommand):
    help = "Load a CSV file exported from another ERP (side-by-side mode)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--source", default="generic", choices=sorted(MAPPINGS))
        parser.add_argument("--object", required=True, choices=ObjectType.values)
        parser.add_argument("--file", required=True)
        parser.add_argument("--user", default="admin", help="Who is loading it (recorded on every document).")

    def handle(self, *args, **options):
        """Run the command under its own request ID, restored afterwards (no leak into what runs next)."""
        with command_request_id():
            return self._run(*args, **options)

    def _run(self, *args, company, source, object, file, user, **options):  # noqa: A002 - the option's name
        company_code = CompanyCode.objects.filter(code=company).first()
        person = get_user_model().objects.filter(username=user).first()
        path = Path(file)
        if company_code is None or person is None:
            raise CommandError(f"Company code {company} or user {user} does not exist.")
        if not path.is_file():
            raise CommandError(f"No file {file}.")
        try:
            batch, created = services.import_file(company_code, source=source, object_type=object,
                                                  content=path.read_bytes(), file_name=path.name, user=person)
        except FileError as exc:
            raise CommandError(str(exc)) from exc
        if created:
            verb = "Loaded"
        elif getattr(batch, "retried", 0):
            verb = (f"Loaded before; retried {batch.retried} rejected row(s), {batch.now_loaded} now loaded. "
                    "In all")
        else:
            verb = "Already loaded (same file, nothing changed)"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {path.name} as {object} from {source}: {batch.rows} row(s), {batch.created} created, "
            f"{batch.updated} updated, {batch.unchanged} unchanged, {batch.rejected} rejected"))
        for result in batch.results:
            if result["status"] == "rejected":
                self.stdout.write(self.style.WARNING(f"  row {result['row']} {result['key']}: {result['detail']}"))
            elif result["detail"]:
                self.stdout.write(f"  row {result['row']} {result['key']}: {result['status']} {result['number']} "
                                  f"({result['detail']})")
