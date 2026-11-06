from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.accounts.models import Role, User
from apps.inventory.models import Product
from apps.sales.models import Customer, SalesOrder, SalesOrderLine

pytestmark = pytest.mark.django_db


def test_string_representations(make_user, pending_order, widget):
    user = make_user(Role.SALES_REP, username="rep", first_name="Thabo", last_name="Nkosi")
    assert str(user) == "Thabo Nkosi (Sales Representative)"
    assert str(widget) == "Widget (W-1)"
    assert str(pending_order.customer) == "Acme"
    assert str(pending_order) == f"Order #{pending_order.pk} - Acme"
    assert str(pending_order.lines.get(product=widget)) == "10 x Widget @ 10.00"


def test_has_role(make_user):
    user = make_user(Role.ACCOUNTANT)
    assert user.has_role(Role.ACCOUNTANT, Role.FINANCE_MANAGER)
    assert not user.has_role(Role.SALES_REP)


def test_line_clean_rejects_zero_quantity(pending_order, widget):
    line = SalesOrderLine(order=pending_order, product=widget, quantity=0, unit_price=1)
    with pytest.raises(ValidationError):
        line.clean()


def test_empty_order_total_is_zero(customer):
    assert SalesOrder.objects.create(customer=customer).total == 0


def test_seed_data_creates_every_role_and_is_repeatable():
    call_command("seed_data", stdout=StringIO())
    call_command("seed_data", stdout=StringIO())  # second run must not duplicate anything

    assert set(User.objects.values_list("role", flat=True)) == set(Role.values)
    assert Product.objects.count() == 2
    assert Customer.objects.count() == 1
    assert SalesOrder.objects.count() == 1
    order = SalesOrder.objects.get()
    assert order.lines.count() == 2
    assert order.created_by.username == "sales_rep"
    assert User.objects.get(username="admin").is_superuser
