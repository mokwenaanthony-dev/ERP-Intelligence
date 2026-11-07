"""Create (or update) the read-only database role BI tools such as Metabase connect with.

Usage (PostgreSQL only):
    python manage.py setup_reporting_role                 # password from REPORTING_DB_PASSWORD in .env
    python manage.py setup_reporting_role --password ...

Least privilege: the role can log in and SELECT from the analytics views and a few lookup tables
(names of accounts, partners, materials, plants, cost and profit centres). It cannot read journal
lines, employees or anything else, and every session it opens is read-only.
"""
from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.analytics.sql import VIEWS, is_postgres

ROLE = "erp_reporting"
LOOKUP_TABLES = [
    "organization_companycode", "organization_plant", "finance_glaccount", "inventory_inventoryaccountsetting",
    "masterdata_businesspartner", "masterdata_material", "controlling_costcenter", "controlling_profitcenter",
]
READABLE = [*VIEWS, *LOOKUP_TABLES]


class Command(BaseCommand):
    help = "Create or update the read-only reporting role for BI tools (PostgreSQL)."

    def add_arguments(self, parser):
        parser.add_argument("--password", help="Defaults to REPORTING_DB_PASSWORD from the environment or .env.")

    @transaction.atomic
    def handle(self, *args, password=None, **options):
        if not is_postgres(connection):
            raise CommandError("The reporting role is for PostgreSQL; SQLite has no database users.")
        password = password or config("REPORTING_DB_PASSWORD", default="")
        if not password:
            raise CommandError("Set REPORTING_DB_PASSWORD in .env or pass --password.")
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [ROLE])
            verb = "ALTER" if cursor.fetchone() else "CREATE"
            # psycopg2 fills in the password client-side, quoted as a literal.
            cursor.execute(f"{verb} ROLE {ROLE} WITH LOGIN PASSWORD %s", [password])
            cursor.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
            cursor.execute(f"GRANT CONNECT ON DATABASE {quote(connection.settings_dict['NAME'])} TO {ROLE}")
            cursor.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
            cursor.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
            cursor.execute(f"GRANT SELECT ON {', '.join(READABLE)} TO {ROLE}")
        action = "Created" if verb == "CREATE" else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{action} role {ROLE}: read-only access to {len(VIEWS)} analytics views and "
            f"{len(LOOKUP_TABLES)} lookup tables."
        ))
