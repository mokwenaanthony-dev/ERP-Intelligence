"""``manage.py setup_demo_portal``: demo portal logins and a shared outlook (Wave 10a).

* ``highveld`` - a person at Highveld Supplies CC (supplier), who may read the demand outlook:
  purchasing agreed to share it with Highveld, the sole steel supplier (the supply risk of 8c);
* ``acme`` - a person at Acme Retail (Pty) Ltd (customer);
* an open, released purchase order for Highveld (50 x TG-1001, due in a week), so the supplier has
  a line to confirm on any fresh database (v0.21.1; before, the demo needed a shell step). Only
  made when Highveld has no open released line, so running the command again adds nothing.

Both users get the seed users' demo password (local demo data only). Running it again changes nothing.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Role
from apps.common.logcontext import command_request_id
from apps.common.management.commands.seed_data import DEFAULT_PASSWORD
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import CompanyCode, Plant, PurchasingOrganization
from apps.portal import services
from apps.portal.management.commands.create_partner_user import link_partner_user
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder, PurchaseOrderItem

User = get_user_model()
DEMO_PASSWORD = DEFAULT_PASSWORD  # local demo data only, like the seed users
DEMO = (("highveld", "Highveld Supplies CC", "SUPPLIER"), ("acme", "Acme Retail (Pty) Ltd", "CUSTOMER"))


class Command(BaseCommand):
    help = "Create the demo portal users (a supplier and a customer) and share the outlook with the supplier."

    def handle(self, *args, **options):
        """Run the command under its own request ID, restored afterwards (no leak into what runs next)."""
        with command_request_id():
            return self._run(*args, **options)

    def _run(self, *args, **options):
        company = CompanyCode.objects.filter(code="1000").first()
        if company is None:
            raise CommandError("Run setup_demo_company first.")
        for username, name, kind in DEMO:
            partner = BusinessPartner.objects.get(name=name)
            access, created = link_partner_user(company_code=company, partner=partner, username=username, kind=kind)
            if created:
                access.user.set_password(DEMO_PASSWORD)
                access.user.save(update_fields=["password"])
            self.stdout.write(f"  {'Created' if created else 'Kept'} portal user {username} -> {partner.number} {name}")
        agreed_by = (User.objects.filter(role=Role.PURCHASING_OFFICER).first()
                     or User.objects.filter(is_superuser=True).first())
        if agreed_by is None:
            raise CommandError("No purchasing officer or administrator to agree the shared outlook: run seed_data.")
        share, changed = services.share(company, BusinessPartner.objects.get(name=DEMO[0][1]), active=True,
                                        user=agreed_by)
        self.stdout.write(f"  Demand outlook {'shared' if changed else 'already shared'} with {share.supplier.number}")
        self._open_order(share.supplier, agreed_by)

    def _open_order(self, supplier, user):
        """A released purchase order with an open line for Highveld, unless one already exists."""
        open_lines = [item for item in PurchaseOrderItem.objects.filter(
            order__supplier=supplier, order__status=PurchaseOrder.Status.RELEASED).select_related("order")
            if item.quantity > item.received_quantity]
        if open_lines:
            self.stdout.write(f"  Highveld already has an open line to confirm: {open_lines[0].order.order_number}/"
                              f"{open_lines[0].item_number}")
            return
        today = timezone.localdate()
        order = purchasing.create_purchase_order(
            purchasing_organization=PurchasingOrganization.objects.get(code="1000"), supplier=supplier,
            order_date=today, delivery_date=today + timedelta(days=7), user=user,
            items=[purchasing.OrderItemInput(Material.objects.get(number="TG-1001"), Decimal("50"), Decimal("180"),
                                             Plant.objects.get(code="1000"))])
        purchasing.release_order(order)
        self.stdout.write(f"  Purchase order {order.order_number} released to Highveld: 50 x TG-1001, due "
                          f"{order.delivery_date}")
