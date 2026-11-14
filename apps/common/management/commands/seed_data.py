"""Seed one user per role, then build the demo company (``setup_demo_company``), its production master
data (``setup_demo_production``), a CRM pipeline (``setup_demo_crm``) and
supplier relationships (``setup_demo_srm``).

Usage:
    python manage.py seed_data

Safe to run more than once: existing records are left alone.
"""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Role

User = get_user_model()

DEFAULT_PASSWORD = "ChangeMe123!"  # local demo data only  # nosec B105

SEED_USERS = [
    ("admin", Role.ADMIN, {"is_staff": True, "is_superuser": True}),
    ("finance_manager", Role.FINANCE_MANAGER, {"is_staff": True}),
    ("accountant", Role.ACCOUNTANT, {}),
    ("sales_rep", Role.SALES_REP, {}),
    ("inventory_manager", Role.INVENTORY_MANAGER, {}),
    ("purchasing_officer", Role.PURCHASING_OFFICER, {}),
    ("hr_admin", Role.HR_ADMIN, {"is_staff": True}),
    ("production_planner", Role.PRODUCTION_PLANNER, {}),
    ("viewer", Role.VIEWER, {}),
]


class Command(BaseCommand):
    help = "Seed users for every role and the demo company with sample data."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("Seeding users...")
        for username, role, extra in SEED_USERS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.com", "role": role, **extra},
            )
            if created:
                user.set_password(DEFAULT_PASSWORD)
                user.save()
                self.stdout.write(self.style.SUCCESS(f"  Created '{username}' ({role.label})"))

        self.stdout.write("Setting up the demo company...")
        call_command("setup_demo_company", stdout=self.stdout)
        self.stdout.write("Setting up demo production master data...")
        call_command("setup_demo_production", stdout=self.stdout)
        self.stdout.write("Setting up a demo CRM pipeline...")
        call_command("setup_demo_crm", stdout=self.stdout)
        self.stdout.write("Setting up demo supplier relationships...")
        call_command("setup_demo_srm", stdout=self.stdout)

        self.stdout.write(self.style.SUCCESS(f"Done. Seed users all use the password: {DEFAULT_PASSWORD}"))
        self.stdout.write(self.style.WARNING("Change these passwords before using this anywhere but your own machine."))
