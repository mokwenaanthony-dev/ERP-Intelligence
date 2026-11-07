"""Analytics: reporting views and management reports.

Scenario (fiscal year 2027, period 1 = March 2026, period 3 = May 2026), on top of the demo
company (2026-03-01: capital R500,000; DR00000001 R10,000 + VAT with no customer or material on the
revenue line; opening stock R27,000):

    2026-05-10  sales order: 10 x TG-1001 @ R199 + 2 x TG-1002 @ R499 = R2,988 net
    2026-05-12  delivery: cost of sales 10 x R120 + 2 x R300 = R1,800
    2026-05-15  invoice: revenue R2,988 + VAT R448.20
    2026-05-31  payroll May: costs R137,386.36 (salaries 135,500, UIF 531.36, SDL 1,355)

On PostgreSQL (CI) the views are materialized, so each test refreshes them before reading.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection

from apps.accounts.models import Role
from apps.analytics import services
from apps.analytics.models import RefreshLog
from apps.analytics.sql import is_postgres
from apps.controlling.models import ProfitCenter
from apps.hr import services as hr
from apps.masterdata.models import Material
from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization, StorageLocation
from apps.purchasing import services as purchasing
from apps.sales import services as sales

pytestmark = pytest.mark.django_db

D = Decimal


def refresh():
    return services.refresh_views()


def material(number):
    return Material.objects.get(number=number)


@pytest.fixture
def scenario(demo):
    order = sales.create_sales_order(
        sales_organization=SalesOrganization.objects.get(code="1000"), customer=demo.customer,
        order_date=date(2026, 5, 10),
        items=[sales.OrderItemInput(material("TG-1001"), D("10")), sales.OrderItemInput(material("TG-1002"), D("2"))],
    )
    sales.confirm_order(order)
    delivery = sales.post_delivery(order, delivery_date=date(2026, 5, 12),
                                   storage_location=StorageLocation.objects.get(code="0001"))
    sales.post_invoice(delivery, invoice_date=date(2026, 5, 15))
    hr.post_payroll_run(demo.company, year=2026, month=5)
    refresh()
    return demo


def test_income_statement(scenario):
    report = services.income_statement(scenario.company, 2027)
    assert [(line["gl_account"], line["amount"]) for line in report["revenue"]] == [("4000", D("12988.00"))]
    assert [(line["gl_account"], line["amount"]) for line in report["cost_of_sales"]] == [("5000", D("1800.00"))]
    assert [(line["gl_account"], line["amount"]) for line in report["operating_expenses"]] == [
        ("6000", D("135500.00")), ("6010", D("531.36")), ("6020", D("1355.00")),
    ]
    assert report["totals"] == {
        "revenue": D("12988.00"), "cost_of_sales": D("1800.00"), "operating_expenses": D("137386.36"),
        "gross_margin": D("11188.00"), "gross_margin_percent": D("86.1"), "result": D("-126198.36"),
    }
    by_period = {row["period"]: row for row in report["by_period"]}
    assert len(by_period) == 12
    assert (by_period[1]["revenue"], by_period[1]["result"]) == (D("10000.00"), D("10000.00"))
    assert (by_period[3]["gross_margin"], by_period[3]["result"]) == (D("1188.00"), D("-136198.36"))
    assert by_period[2]["result"] == 0
    assert report["materialized"] is is_postgres(connection)


def test_income_statement_by_profit_centre_and_period(scenario):
    trading = ProfitCenter.objects.get(code="PC1000")
    report = services.income_statement(scenario.company, 2027, profit_center=trading)
    assert report["totals"]["revenue"] == D("2988.00")  # DR00000001 has no profit centre
    assert report["totals"]["operating_expenses"] == D("51359.24")  # sales and warehouse cost centres
    assert report["totals"]["result"] == D("-50171.24")
    assert report["profit_center"] == "PC1000"
    only_march = services.income_statement(scenario.company, 2027, 1, 1)
    assert only_march["totals"]["result"] == D("10000.00")
    assert len(only_march["by_period"]) == 1
    assert services.income_statement(scenario.company, 2026)["totals"]["revenue"] == 0


def test_balance_sheet_balances(scenario):
    sheet = services.balance_sheet(scenario.company, 2027, 3)
    assert [(line["gl_account"], line["amount"]) for line in sheet["assets"]] == [
        ("1000", D("500000.00")), ("1100", D("14936.20")), ("1200", D("25200.00")),
    ]
    assert [(line["gl_account"], line["amount"]) for line in sheet["liabilities"]] == [
        ("2100", D("1948.20")), ("2400", D("31757.50")), ("2410", D("1062.72")), ("2420", D("1355.00")),
        ("2500", D("103211.14")),
    ]
    assert [(line["gl_account"], line["amount"]) for line in sheet["equity"]] == [
        ("3000", D("500000.00")), ("3200", D("27000.00")), (None, D("-126198.36")),
    ]
    assert sheet["totals"] == {"assets": D("540136.20"), "liabilities": D("139334.56"), "equity": D("400801.64"),
                               "liabilities_and_equity": D("540136.20")}
    assert sheet["balanced"] is True
    march = services.balance_sheet(scenario.company, 2027, 1)
    assert march["balanced"] is True
    assert march["equity"][-1]["amount"] == D("10000.00")
    later_year = services.balance_sheet(scenario.company, 2028, 1)  # earlier years carry forward
    assert later_year["totals"]["assets"] == D("540136.20")


@pytest.mark.parametrize("group_by, expected", [
    ("customer", [(None, D("10000.00"), D("0.00"), D("100.0")),
                  ("CUSTOMER", D("2988.00"), D("1800.00"), D("39.8"))]),  # Acme; its number depends on the database
    ("material", [(None, D("10000.00"), D("0.00"), D("100.0")),
                  ("TG-1001", D("1990.00"), D("1200.00"), D("39.7")),
                  ("TG-1002", D("998.00"), D("600.00"), D("39.9"))]),
    ("profit_center", [(None, D("10000.00"), D("0.00"), D("100.0")),
                       ("PC1000", D("2988.00"), D("1800.00"), D("39.8"))]),
])
def test_sales_margin(scenario, group_by, expected):
    report = services.sales_margin(scenario.company, 2027, group_by=group_by)
    # Partner numbers come from the row id, and PostgreSQL does not roll back id sequences between tests.
    expected = [(scenario.customer.number if key == "CUSTOMER" else key, *rest) for key, *rest in expected]
    assert [(r["key"], r["revenue"], r["cost_of_sales"], r["margin_percent"]) for r in report["rows"]] == expected
    assert report["totals"]["gross_margin"] == D("11188.00")
    assert report["rows"][0]["name"] == "Not assigned"


def test_sales_margin_rejects_unknown_grouping(demo):
    with pytest.raises(ValueError, match="group_by"):
        services.sales_margin(demo.company, 2027, group_by="region")


def test_stock_ageing(scenario):
    report = services.stock_ageing(scenario.company, as_of=date(2026, 7, 1))
    assert [(r["material"], r["quantity"], r["value"], r["age_days"], r["age_bucket"], r["slow_moving"])
            for r in report["rows"]] == [
        ("TG-1001", D("90.000"), D("10800.00"), 122, "91-180", False),
        ("TG-1002", D("48.000"), D("14400.00"), 122, "91-180", False),
    ]
    assert report["rows"][0]["last_issue_date"] == date(2026, 5, 12)
    assert report["total_value"] == D("25200.00")
    assert report["value_by_age"]["91-180"] == D("25200.00")
    later = services.stock_ageing(scenario.company, as_of=date(2026, 9, 30))
    assert {r["age_bucket"] for r in later["rows"]} == {"over_180"}
    assert later["slow_moving_value"] == D("25200.00")  # nothing issued for 141 days


def test_stock_ageing_after_a_purchase_and_an_empty_material(demo):
    order = purchasing.create_purchase_order(
        purchasing_organization=PurchasingOrganization.objects.get(code="1000"), supplier=demo.supplier,
        order_date=date(2026, 6, 10),
        items=[purchasing.OrderItemInput(material("TG-1001"), D("10"), D("125.00"))],
    )
    purchasing.post_goods_receipt(purchasing.release_order(order), receipt_date=date(2026, 6, 15))
    from apps.inventory import services as inventory
    from apps.inventory.models import MovementType

    inventory.post_goods_movement(
        movement_type=MovementType.SCRAPPING, plant=Plant.objects.get(code="1000"), posting_date=date(2026, 6, 20),
        items=[inventory.ItemInput(material("TG-1002"), StorageLocation.objects.get(code="0001"), D("50"))],
    )
    refresh()
    report = services.stock_ageing(demo.company, as_of=date(2026, 7, 1), slow_after_days=10)
    [row] = report["rows"]  # TG-1002 is fully scrapped, so it drops out
    summary = (row["material"], row["quantity"], row["age_days"], row["age_bucket"])
    assert summary == ("TG-1001", D("110.000"), 16, "0-30")
    assert row["slow_moving"] is True  # never issued


@pytest.mark.parametrize("days, bucket", [(None, "no_receipt"), (0, "0-30"), (30, "0-30"), (31, "31-90"),
                                          (90, "31-90"), (180, "91-180"), (181, "over_180")])
def test_age_buckets(days, bucket):
    assert services._age_bucket(days) == bucket


def test_payroll_cost_trend(scenario):
    run_june = hr.post_payroll_run(scenario.company, year=2026, month=6)
    refresh()
    trend = services.payroll_cost_trend(scenario.company, 2026)
    assert [(r["month"], r["headcount"], r["gross"], r["employer_contributions"], r["total_cost"])
            for r in trend["rows"]] == [
        (5, 3, D("135500.00"), D("1886.36"), D("137386.36")),
        (6, 3, D("135500.00"), D("1886.36"), D("137386.36")),
    ]
    by_centre = services.payroll_cost_trend(scenario.company, 2026, by_cost_center=True)
    assert [(r["cost_center"], r["total_cost"]) for r in by_centre["rows"] if r["month"] == 5] == [
        ("1000", D("86027.12")), ("2000", D("32497.12")), ("3000", D("18862.12")),
    ]
    hr.reverse_payroll_run(run_june)
    refresh()
    assert [r["month"] for r in services.payroll_cost_trend(scenario.company)["rows"]] == [5]


def test_refresh_and_freshness(demo):
    results = refresh()
    if is_postgres(connection):
        assert [r["view"] for r in results] == ["analytics_gl_period_balance", "analytics_sales_margin",
                                                "analytics_stock_position", "analytics_payroll_cost"]
        assert RefreshLog.objects.count() == 4
        assert services.income_statement(demo.company, 2027)["data_as_of"] is not None
        assert str(RefreshLog.objects.first()).startswith("analytics_gl_period_balance refreshed")
    else:
        assert results == []
        assert services.income_statement(demo.company, 2027)["data_as_of"] is None
    state = services.freshness()
    assert [v["view"] for v in state["views"]] == list(services.VIEWS)


def test_refresh_command_and_task(demo):
    from io import StringIO

    from django.core.management import call_command

    from apps.analytics.tasks import refresh_views

    out = StringIO()
    call_command("refresh_analytics", stdout=out)
    assert ("Refreshed" if is_postgres(connection) else "always current") in out.getvalue()
    assert len(refresh_views()) == (4 if is_postgres(connection) else 0)


# --- API --------------------------------------------------------------------------------------

def test_report_endpoints(scenario, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    base = {"company_code": "1000", "fiscal_year": 2027}
    statement = accountant.get("/api/analytics/income-statement/", {**base, "profit_center": "PC1000"})
    assert statement.status_code == 200, statement.data
    assert statement.data["totals"]["revenue"] == D("2988.00")
    sheet = accountant.get("/api/analytics/balance-sheet/", {**base, "period": 3})
    assert sheet.data["balanced"] is True
    margin = accountant.get("/api/analytics/sales-margin/", {**base, "group_by": "material"})
    assert margin.data["rows"][1]["key"] == "TG-1001"
    stock = accountant.get("/api/analytics/stock-ageing/", {"company_code": "1000", "as_of": "2026-07-01"})
    assert stock.data["total_value"] == D("25200.00")
    manager = client_for(Role.FINANCE_MANAGER)
    params = {"company_code": "1000", "year": 2026, "by_cost_center": "true"}
    payroll = manager.get("/api/analytics/payroll-cost/", params)
    assert len(payroll.data["rows"]) == 3
    assert accountant.get("/api/analytics/refresh/").status_code == 200
    refreshed = manager.post("/api/analytics/refresh/")
    assert refreshed.status_code == 200
    assert "refreshed" in refreshed.data


def test_report_input_errors(demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    assert accountant.get("/api/analytics/income-statement/", {"company_code": "1000"}).status_code == 400
    assert accountant.get("/api/analytics/income-statement/", {
        "company_code": "1000", "fiscal_year": 2027, "profit_center": "NOPE"}).status_code == 400
    assert accountant.get("/api/analytics/income-statement/", {
        "company_code": "1000", "fiscal_year": 2027, "period_from": 5, "period_to": 2}).status_code == 400
    assert accountant.get("/api/analytics/balance-sheet/", {
        "company_code": "1000", "fiscal_year": 2027, "period": 13}).status_code == 400
    assert accountant.get("/api/analytics/sales-margin/", {
        "company_code": "1000", "fiscal_year": 2027, "group_by": "region"}).status_code == 400


@pytest.mark.parametrize("role, path, allowed", [
    (Role.SALES_REP, "sales-margin", True), (Role.SALES_REP, "income-statement", False),
    (Role.INVENTORY_MANAGER, "stock-ageing", True), (Role.INVENTORY_MANAGER, "sales-margin", False),
    (Role.PURCHASING_OFFICER, "stock-ageing", True), (Role.HR_ADMIN, "payroll-cost", True),
    (Role.HR_ADMIN, "balance-sheet", False), (Role.ACCOUNTANT, "payroll-cost", False),
    (Role.VIEWER, "income-statement", False),
])
def test_report_access(demo, client_for, role, path, allowed):
    response = client_for(role).get(f"/api/analytics/{path}/", {"company_code": "1000", "fiscal_year": 2027})
    assert (response.status_code != 403) is allowed


def test_only_the_finance_manager_refreshes(demo, client_for):
    assert client_for(Role.ACCOUNTANT).post("/api/analytics/refresh/").status_code == 403
