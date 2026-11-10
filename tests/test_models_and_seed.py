from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role, User
from apps.finance.models import JournalEntry
from apps.inventory.models import MaterialDocument
from apps.organization.models import CompanyCode
from apps.production.models import BillOfMaterials, WorkCenter
from apps.sales.models import SalesPrice

pytestmark = pytest.mark.django_db


def test_user_string_and_roles(make_user):
    user = make_user(Role.SALES_REP, username="rep", first_name="Thabo", last_name="Nkosi")
    assert str(user) == "Thabo Nkosi (Sales Representative)"
    assert user.has_role(Role.SALES_REP, Role.ACCOUNTANT)
    assert not user.has_role(Role.FINANCE_MANAGER)


def test_seed_data_creates_every_role_and_the_demo_company_and_is_repeatable():
    call_command("seed_data", stdout=StringIO())
    call_command("seed_data", stdout=StringIO())  # second run must not duplicate anything

    assert set(User.objects.values_list("role", flat=True)) == set(Role.values)
    assert User.objects.get(username="admin").is_superuser
    assert CompanyCode.objects.count() == 1
    # Demo company: capital, invoice, opening stock. Demo production: opening stock of raw materials.
    assert JournalEntry.objects.count() == 4
    assert MaterialDocument.objects.count() == 2
    assert SalesPrice.objects.count() == 3  # TG-1001, TG-1002 and FG-4001
    assert WorkCenter.objects.count() == 2 and BillOfMaterials.objects.count() == 2
