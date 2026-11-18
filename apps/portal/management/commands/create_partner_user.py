"""``manage.py create_partner_user --company 1000 --partner 1000002 --username highveld [--kind SUPPLIER]``.

Creates a portal login for a person at a supplier or customer and links it to that business partner.
No password is set here (a password typed on a command line ends up in the shell history): the
command says how to set one with ``manage.py changepassword``. Running it again for the same user
and partner changes nothing; a user already linked to another partner is refused.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Role
from apps.common.logcontext import command_request_id
from apps.masterdata.models import BusinessPartner, CustomerRole, SupplierRole
from apps.organization.models import CompanyCode
from apps.portal.models import PartnerAccess

User = get_user_model()


def link_partner_user(*, company_code, partner, username: str, kind: str | None = None, email: str = ""):
    """Create (or find) the portal user and its link; returns (access, created). Shared with the demo."""
    is_supplier = SupplierRole.objects.filter(business_partner=partner, company_code=company_code).exists()
    is_customer = CustomerRole.objects.filter(business_partner=partner, company_code=company_code).exists()
    if kind is None:
        if is_supplier == is_customer:
            raise CommandError(f"{partner.number} is {'both' if is_supplier else 'neither'} a supplier and a "
                               f"customer in {company_code.code}: give --kind SUPPLIER or CUSTOMER.")
        kind = PartnerAccess.Kind.SUPPLIER if is_supplier else PartnerAccess.Kind.CUSTOMER
    allowed = is_supplier if kind == PartnerAccess.Kind.SUPPLIER else is_customer
    if not allowed:
        raise CommandError(f"{partner.number} is not a {kind.lower()} in company code {company_code.code}.")
    role = Role.SUPPLIER if kind == PartnerAccess.Kind.SUPPLIER else Role.CUSTOMER
    with transaction.atomic():
        user = User.objects.filter(username=username).first()
        if user is None:
            user = User(username=username, email=email, role=role)
            user.set_unusable_password()
            user.save()
        elif user.role != role:
            raise CommandError(f"User {username} exists with role {user.get_role_display()}; choose another name.")
        access = PartnerAccess.objects.filter(user=user).first()
        if access:
            if (access.partner_id, access.company_code_id, access.kind) != (partner.pk, company_code.pk, kind):
                raise CommandError(f"User {username} is already linked to {access.partner.number}.")
            return access, False
        return PartnerAccess.objects.create(user=user, company_code=company_code, partner=partner, kind=kind), True


class Command(BaseCommand):
    help = "Create a portal login for a supplier's or customer's person, linked to that business partner."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--partner", required=True, help="Business partner number.")
        parser.add_argument("--username", required=True)
        parser.add_argument("--kind", choices=PartnerAccess.Kind.values, help="Only needed for a partner that "
                                                                              "is both supplier and customer.")
        parser.add_argument("--email", default="")

    def handle(self, *args, **options):
        """Run the command under its own request ID, restored afterwards (no leak into what runs next)."""
        with command_request_id():
            return self._run(*args, **options)

    def _run(self, *args, company, partner, username, kind, email, **options):
        company_code = CompanyCode.objects.filter(code=company).first()
        business_partner = BusinessPartner.objects.filter(number=partner).first()
        if company_code is None or business_partner is None:
            raise CommandError(f"Company code {company} or business partner {partner} does not exist.")
        access, created = link_partner_user(company_code=company_code, partner=business_partner, username=username,
                                            kind=kind, email=email)
        verb = "Created" if created else "Already linked (unchanged)"
        self.stdout.write(self.style.SUCCESS(f"{verb}: {username} -> {business_partner.number} "
                                             f"{business_partner.name} ({access.kind.lower()})"))
        if created:
            self.stdout.write(f"Set a password with: python manage.py changepassword {username}")
