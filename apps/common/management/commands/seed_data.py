"""Seed one user per role plus sample products, a customer and an order.

Usage:
    python manage.py seed_data

Safe to run more than once: existing records are left alone.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Role
from apps.inventory.models import Product
from apps.sales.models import Customer, SalesOrder, SalesOrderLine

User = get_user_model()

DEFAULT_PASSWORD = "ChangeMe123!"  # nosec B105 - local demo data only

SEED_USERS = [
    ("admin", Role.ADMIN, {"is_staff": True, "is_superuser": True}),
    ("finance_manager", Role.FINANCE_MANAGER, {"is_staff": True}),
    ("accountant", Role.ACCOUNTANT, {}),
    ("sales_rep", Role.SALES_REP, {}),
    ("inventory_manager", Role.INVENTORY_MANAGER, {}),
    ("purchasing_officer", Role.PURCHASING_OFFICER, {}),
    ("hr_admin", Role.HR_ADMIN, {"is_staff": True}),
    ("viewer", Role.VIEWER, {}),
]


class Command(BaseCommand):
    help = "Seed users for every role and sample ERP data for local testing."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("Seeding users...")
        users = {}
        for username, role, extra in SEED_USERS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.com", "role": role, **extra},
            )
            if created:
                user.set_password(DEFAULT_PASSWORD)
                user.save()
                self.stdout.write(self.style.SUCCESS(f"  Created '{username}' ({role.label})"))
            users[username] = user

        inventory_manager = users["inventory_manager"]
        sales_rep = users["sales_rep"]

        self.stdout.write("Seeding sample products...")
        widget, _ = Product.objects.get_or_create(
            sku="WIDGET-001",
            defaults={"name": "Standard Widget", "price": "19.99", "stock": 500,
                      "created_by": inventory_manager, "updated_by": inventory_manager},
        )
        gadget, _ = Product.objects.get_or_create(
            sku="GADGET-002",
            defaults={"name": "Deluxe Gadget", "price": "49.99", "stock": 200,
                      "created_by": inventory_manager, "updated_by": inventory_manager},
        )

        self.stdout.write("Seeding sample customer and order...")
        customer, _ = Customer.objects.get_or_create(
            email="customer@example.com",
            defaults={"name": "Acme Corp", "phone": "011-555-0100", "address": "123 Main St, Johannesburg",
                      "created_by": sales_rep, "updated_by": sales_rep},
        )
        if not customer.orders.exists():
            order = SalesOrder.objects.create(customer=customer, created_by=sales_rep, updated_by=sales_rep)
            for product, quantity in ((widget, 10), (gadget, 2)):
                SalesOrderLine.objects.create(
                    order=order, product=product, quantity=quantity, created_by=sales_rep, updated_by=sales_rep
                )
            self.stdout.write(self.style.SUCCESS(f"  Created sample order #{order.pk}"))

        self.stdout.write(self.style.SUCCESS(f"Done. Seed users all use the password: {DEFAULT_PASSWORD}"))
        self.stdout.write(self.style.WARNING("Change these passwords before using this anywhere but your own machine."))
