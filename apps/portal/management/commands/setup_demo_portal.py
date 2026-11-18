"""``manage.py setup_demo_portal``: demo portal logins and a shared outlook (Wave 10a).

* ``highveld`` - a person at Highveld Supplies CC (supplier), who may read the demand outlook:
  purchasing agreed to share it with Highveld, the sole steel supplier (the supply risk of 8c);
* ``acme`` - a person at Acme Retail (Pty) Ltd (customer).

Both use the seed users' demo password (local demo data only). Running it again changes nothing.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role
from apps.common.logcontext import command_request_id
from apps.common.management.commands.seed_data import DEFAULT_PASSWORD
from apps.masterdata.models import BusinessPartner
from apps.organization.models import CompanyCode
from apps.portal import services
from apps.portal.management.commands.create_partner_user import link_partner_user

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
