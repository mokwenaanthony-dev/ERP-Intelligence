"""Supply-chain risk and forecast accuracy (Wave 8c), and the 15-minute MRP schedule."""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command

from apps.accounts.models import Role
from apps.inventory import services as inventory
from apps.inventory.models import MovementType
from apps.inventory.services import ItemInput
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization, StorageLocation
from apps.planning import risk
from apps.planning.models import ForecastItem, ForecastPeriod, ForecastRun, MaterialPlanning
from apps.purchasing import services as purchasing
from apps.sales import services as sales
from apps.srm import services as srm

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/planning"
TODAY = date(2026, 11, 15)


@pytest.fixture
def chain(demo):
    """The demo product chain with steel (RM-2001) bought only from Highveld, and an order for 450 brackets."""
    call_command("setup_demo_production", stdout=StringIO())
    plant = Plant.objects.get(code="1000")
    demo.plant, demo.org = plant, PurchasingOrganization.objects.get(code="1000")
    demo.steel, demo.fasteners = Material.objects.get(number="RM-2001"), Material.objects.get(number="RM-2002")
    MaterialPlanning.objects.create(material=demo.steel, plant=plant, procurement="BUY", lead_time_days=10,
                                    preferred_supplier=demo.supplier)
    demo.order = sales.create_sales_order(
        sales_organization=SalesOrganization.objects.get(code="1000"), customer=demo.customer, order_date=TODAY,
        requested_delivery_date=date(2026, 12, 15),
        items=[sales.OrderItemInput(material=Material.objects.get(number="FG-4001"), quantity=D("450"), plant=plant)])
    return demo


def row_for(rows, supplier):
    return next(r for r in rows if r["supplier"] == supplier.number)


def test_a_single_source_without_contract_or_grade_is_high_risk(chain):
    [row] = risk.supplier_risk(chain.company, on=TODAY)
    assert (row["supplier"], row["risk"], row["grade"]) == (chain.supplier.number, "HIGH", None)
    assert row["materials"] == [{"material": "RM-2001", "single_source": True, "on_contract": False,
                                 "used_in": ["FG-4001", "SF-3001"]}]  # steel -> frame -> bracket
    assert row["revenue_at_risk"] == row["exposed_revenue"] == D("202500.00")  # 450 x R450
    assert row["orders_at_risk"] == [chain.order.order_number]
    assert "Qualify a second source for RM-2001" in row["recommendation"]
    assert "Agree a contract for RM-2001" in row["recommendation"] and "Evaluate the supplier" in row["recommendation"]


def test_a_second_source_moves_the_risk(chain):
    second = srm.approve_application(srm.submit_application(
        company_code=chain.company, company_name="Steel Two (Pty) Ltd", registration_number="2020/1/07",
        vat_number="4666666666")).business_partner
    purchasing.create_purchase_order(purchasing_organization=chain.org, supplier=second, order_date=TODAY,
                                     items=[purchasing.OrderItemInput(material=chain.steel, quantity=D("10"),
                                                                      net_price=D("40"), plant=chain.plant)])
    rows = risk.supplier_risk(chain.company, on=TODAY)
    for supplier in (chain.supplier, second):
        row = row_for(rows, supplier)
        # Both are exposed to the order, but neither alone can stop it.
        assert (row["risk"], row["exposed_revenue"], row["revenue_at_risk"]) == ("LOW", D("202500.00"), D("0.00"))
        assert row["recommendation"] == "Every material it supplies has another source."


def test_a_contract_and_a_good_grade_make_it_medium(chain):
    srm.create_contract(purchasing_organization=chain.org, supplier=chain.supplier, valid_from=date(2026, 11, 1),
                        valid_to=date(2027, 2, 28), items=[srm.ContractItemInput(chain.steel, D("38"), D("5000"))])
    srm.evaluate_supplier(company_code=chain.company, supplier=chain.supplier, period_start=date(2026, 3, 1),
                          period_end=date(2026, 10, 31), quality_score=5, evaluated_on=date(2026, 11, 1))
    [row] = risk.supplier_risk(chain.company, on=TODAY)
    assert (row["risk"], row["grade"], row["materials"][0]["on_contract"]) == ("MEDIUM", "A", True)
    assert row["recommendation"] == "Qualify a second source for RM-2001 (run an RFQ)."


def test_the_horizon_limits_the_orders_counted(chain):
    [row] = risk.supplier_risk(chain.company, on=TODAY, months=1)  # November only: the order is for December
    assert (row["revenue_at_risk"], row["risk"]) == (D("0.00"), "LOW")
    assert "no open orders depend on it" in row["recommendation"]


# --- forecast accuracy -----------------------------------------------------------------------------------

def forecast(plant, material, first_period, quantities, number):
    run = ForecastRun.objects.create(company_code=plant.company_code, plant=plant, run_number=number,
                                     first_period=first_period, horizon=len(quantities), history_months=12,
                                     model_version="0.1.0", fingerprint=number)
    item = ForecastItem.objects.create(run=run, material=material, method="naive", history_total=D("0"))
    for i, quantity in enumerate(quantities):
        month = date(first_period.year + (first_period.month - 1 + i) // 12, (first_period.month - 1 + i) % 12 + 1, 1)
        ForecastPeriod.objects.create(item=item, period=month, quantity=D(quantity), lower=0, upper=D(quantity))
    return run


def test_forecast_accuracy_uses_the_forecast_available_at_the_time(demo):
    plant = Plant.objects.get(code="1000")
    widget = Material.objects.get(number="TG-1001")
    main = StorageLocation.objects.get(plant=plant, code="0001")
    inventory.post_goods_movement(movement_type=MovementType.INITIAL_ENTRY, plant=plant, posting_date=date(2026, 3, 1),
                                  items=[ItemInput(widget, main, D("100"), D("180"))])
    for month, quantity in ((8, "10"), (9, "20"), (10, "30")):
        inventory.post_goods_movement(movement_type=MovementType.GOODS_ISSUE_DELIVERY, plant=plant,
                                      posting_date=date(2026, month, 5), items=[ItemInput(widget, main, D(quantity))])
    forecast(plant, widget, date(2026, 8, 1), ["12", "12", "12"], "FR00000001")
    forecast(plant, widget, date(2026, 10, 1), ["25"], "FR00000002")  # newer, available from October
    forecast(plant, widget, date(2026, 11, 1), ["99"], "FR00000003")  # made after October: not used for it
    [row] = risk.forecast_accuracy(plant, on=TODAY, months=3)
    assert [(p["period"], p["forecast"], p["actual"], p["run"]) for p in row["points"]] == [
        ("2026-08", D("12.000"), D("10.000"), "FR00000001"), ("2026-09", D("12.000"), D("20.000"), "FR00000001"),
        ("2026-10", D("25.000"), D("30.000"), "FR00000002")]
    # Errors +2, -8, -5: MAE 5, bias -11/3 (forecasts too low), accuracy 1 - 15/60 = 75 %.
    assert (row["mae"], row["bias"], row["accuracy_percent"], row["months"]) == (D("5.000"), D("-3.667"),
                                                                                 D("75.0"), 3)
    assert risk.forecast_accuracy(plant, on=date(2026, 8, 20), months=3) == []  # no complete forecast month yet


# --- API, command, schedule ------------------------------------------------------------------------------

def test_the_api_and_roles(chain, client_for):
    buyer = client_for(Role.PURCHASING_OFFICER)
    answer = buyer.get(f"{API}/supplier-risk/?company_code=1000&on=2026-11-15").json()
    assert answer["revenue_at_risk"] == "202500.00" and answer["suppliers"][0]["risk"] == "HIGH"
    assert buyer.get(f"{API}/forecast-accuracy/?plant=1000&on=2026-11-15").json()["materials"] == []
    assert buyer.get(f"{API}/supplier-risk/").status_code == 400  # company_code is required
    assert client_for(Role.VIEWER).get(f"{API}/supplier-risk/?company_code=1000").status_code == 200
    assert client_for(Role.HR_ADMIN).get(f"{API}/supplier-risk/?company_code=1000").status_code == 403


def test_the_command(chain):
    out = StringIO()
    call_command("supply_risk", "--company", "1000", stdout=out)
    assert "HIGH" in out.getvalue() and "RM-2001: single source; used in FG-4001, SF-3001" in out.getvalue()


def test_mrp_is_rechecked_every_fifteen_minutes():
    schedule = django_settings.CELERY_BEAT_SCHEDULE["mrp-every-15-minutes"]
    assert schedule["task"] == "planning.mrp_all_plants" and str(schedule["schedule"]._orig_minute) == "*/15"


def test_unknown_company_in_the_command(db):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="does not exist"):
        call_command("supply_risk", "--company", "9999", stdout=StringIO())
    assert BusinessPartner.objects.count() == 0
