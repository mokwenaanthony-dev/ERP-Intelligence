"""The demo activity generator and the read-only reporting role for BI tools."""
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from apps.analytics import services as analytics
from apps.analytics.sql import is_postgres
from apps.finance import services as finance
from apps.hr.models import PayrollRun
from apps.inventory import services as inventory
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder, SupplierInvoice
from apps.sales.models import Invoice, SalesOrder

pytestmark = pytest.mark.django_db


def generate(months=2, **kwargs):
    out = StringIO()
    call_command("seed_demo_activity", "--months", str(months), stdout=out, **kwargs)
    return out.getvalue()


def test_two_months_of_activity_keep_every_ledger_in_balance(demo):
    output = generate(2)
    assert "2026-03 posted" in output and "2026-04 posted" in output
    assert PurchaseOrder.objects.count() == 4  # two suppliers a month
    assert SupplierInvoice.objects.count() == 4
    assert SalesOrder.objects.count() >= 25
    assert Invoice.objects.count() == SalesOrder.objects.count()
    assert PayrollRun.objects.count() == 2

    company = demo.company
    assert finance.trial_balance(company, 2027)["balanced"] is True
    assert finance.receivables_reconciliation(company)["reconciled"] is True
    assert finance.payables_reconciliation(company)["reconciled"] is True
    assert inventory.stock_reconciliation(company)["reconciled"] is True
    assert purchasing.gr_ir_report(company)["reconciled"] is True

    analytics.refresh_views()
    statement = analytics.income_statement(company, 2027, 1, 2)
    assert statement["totals"]["revenue"] > 0 and statement["totals"]["cost_of_sales"] > 0
    assert analytics.balance_sheet(company, 2027, 2)["balanced"] is True
    # March supplier invoices were paid in April; April's are still open.
    open_payables = finance.open_items(company, reconciliation_type="SUPPLIER")
    assert {item["posting_date"].month for item in open_payables} == {4}
    # VAT for March was settled with SARS in April, so only April's VAT is still on the books.
    from apps.finance.models import JournalEntry

    assert JournalEntry.objects.filter(header_text="SARS VAT201 settlement").count() == 1


def test_same_seed_same_data(demo):
    generate(1, seed=7)
    first = sorted(SalesOrder.objects.values_list("items__quantity", flat=True))
    assert first  # something was sold
    assert all(quantity > 0 for quantity in first)


def test_activity_is_generated_only_once(demo):
    generate(1)
    with pytest.raises(CommandError, match="only once"):
        generate(1)


def test_needs_the_demo_company(db):
    with pytest.raises(CommandError, match="seed_data"):
        generate(1)


def test_needs_a_completed_month(demo, monkeypatch):
    import datetime

    from apps.common.management.commands import seed_demo_activity

    class EarlyDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 15)  # the first month of fiscal year 2027 is not over yet

    monkeypatch.setattr(seed_demo_activity, "date", EarlyDate)
    with pytest.raises(CommandError, match="No completed month"):
        call_command("seed_demo_activity", stdout=StringIO())


def test_reporting_role(demo, monkeypatch):
    if not is_postgres(connection):
        with pytest.raises(CommandError, match="PostgreSQL"):
            call_command("setup_reporting_role", "--password", "x", stdout=StringIO())
        return
    monkeypatch.delenv("REPORTING_DB_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="REPORTING_DB_PASSWORD"):
        call_command("setup_reporting_role", stdout=StringIO())
    out = StringIO()
    call_command("setup_reporting_role", "--password", "test-only-password", stdout=out)
    assert "Created role erp_reporting" in out.getvalue()
    call_command("setup_reporting_role", "--password", "changed", stdout=out)
    assert "Altered role erp_reporting" in out.getvalue()
    with connection.cursor() as cursor:
        def can_read(table):
            cursor.execute("SELECT has_table_privilege('erp_reporting', %s, 'SELECT')", [table])
            return cursor.fetchone()[0]

        assert can_read("analytics_gl_period_balance") and can_read("masterdata_material")
        assert not can_read("finance_journalline")
        assert not can_read("hr_employee")
