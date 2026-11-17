"""Control tower (Wave 9a): KPIs with status, the problems inbox, roles, the API and the command.

One scenario on the demo company, judged on 15 November 2026:
* two customer order lines were due in the last 90 days: one delivered on time, one still open (late);
* two purchase order lines were due: one received on time, one not received (overdue);
* press PRESS3 is down; an urgent ticket is overdue; steel is single source for an open order.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role
from apps.control import services
from apps.crm import services as crm
from apps.masterdata.models import Material
from apps.organization.models import Plant, PurchasingOrganization, SalesOrganization, StorageLocation
from apps.planning.models import MaterialPlanning
from apps.production.models import CellState, WorkCenter
from apps.purchasing import services as purchasing
from apps.sales import services as sales

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/control"
ON = date(2026, 11, 15)
NOW = datetime(2026, 11, 15, 8, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def tower(demo):
    call_command("setup_demo_production", stdout=StringIO())
    plant = Plant.objects.get(code="1000")
    main = StorageLocation.objects.get(plant=plant, code="0001")
    widget = Material.objects.get(number="TG-1001")
    org, sorg = PurchasingOrganization.objects.get(code="1000"), SalesOrganization.objects.get(code="1000")

    # Stock to sell: 100 widgets received on 1 October, on time (due 1 October).
    on_time = purchasing.create_purchase_order(
        purchasing_organization=org, supplier=demo.supplier, order_date=date(2026, 9, 20),
        delivery_date=date(2026, 10, 1), items=[purchasing.OrderItemInput(widget, D("100"), D("180"), plant)])
    purchasing.post_goods_receipt(purchasing.release_order(on_time), receipt_date=date(2026, 10, 1))
    # A second purchase order, due 1 November, never received.
    late_po = purchasing.create_purchase_order(
        purchasing_organization=org, supplier=demo.supplier, order_date=date(2026, 10, 20),
        delivery_date=date(2026, 11, 1), items=[purchasing.OrderItemInput(widget, D("50"), D("180"), plant)])
    purchasing.release_order(late_po)

    # Customer order 1, due 10 October, delivered on 8 October; order 2, due 5 November, not delivered.
    first = sales.create_sales_order(sales_organization=sorg, customer=demo.customer, order_date=date(2026, 10, 1),
                                     requested_delivery_date=date(2026, 10, 10),
                                     items=[sales.OrderItemInput(material=widget, quantity=D("40"), plant=plant)])
    sales.confirm_order(first)
    sales.post_delivery(first, delivery_date=date(2026, 10, 8), storage_location=main)
    late_so = sales.create_sales_order(sales_organization=sorg, customer=demo.customer, order_date=date(2026, 10, 20),
                                       requested_delivery_date=date(2026, 11, 5),
                                       items=[sales.OrderItemInput(material=widget, quantity=D("10"), plant=plant)])

    # The shop floor and service.
    press = WorkCenter.objects.get(plant=plant, code="PRESS3")
    CellState.objects.create(work_center=press, state=CellState.State.DOWN, since=NOW - timedelta(hours=2),
                             reported_at=NOW - timedelta(hours=2))
    crm.open_ticket(company_code=demo.company, customer=demo.customer, subject="Bracket cracked",
                    priority="URGENT", opened_at=NOW - timedelta(days=1))

    # Steel only from Highveld, and an order for brackets that need it.
    MaterialPlanning.objects.create(material=Material.objects.get(number="RM-2001"), plant=plant, procurement="BUY",
                                    lead_time_days=10, preferred_supplier=demo.supplier)
    sales.create_sales_order(sales_organization=sorg, customer=demo.customer, order_date=ON,
                             requested_delivery_date=date(2026, 12, 15),
                             items=[sales.OrderItemInput(material=Material.objects.get(number="FG-4001"),
                                                         quantity=D("10"), plant=plant)])
    demo.late_so, demo.late_po = late_so, late_po
    return demo


def kpi(result, key):
    return next(k for k in result["kpis"] if k["key"] == key)


def test_kpis_now_and_ahead(tower):
    result = services.kpis(tower.company, on=ON)
    customers, suppliers = kpi(result, "otif_customers"), kpi(result, "otif_suppliers")
    assert (customers["value"], customers["status"]) == (D("50.0"), "critical")  # 1 of 2 lines, target 95
    assert "1 of 2 order lines" in customers["basis"]
    assert (suppliers["value"], suppliers["status"]) == (D("50.0"), "critical")
    days = kpi(result, "inventory_days")
    # Stock value today against the cost of the 40 widgets delivered in the last 90 days (at moving average).
    stock, cost = basis_amounts(days["basis"])
    assert cost > 0 and days["value"] == (stock / (cost / 90)).quantize(D("0.1"))
    assert days["status"] == "critical"  # far above the 45-day target: little has been sold yet
    risk = kpi(result, "revenue_at_risk")
    assert risk["value"] > 0 and risk["status"] == "critical"  # Highveld is ungraded and has no steel contract
    assert kpi(result, "forecast_accuracy")["value"] is None and kpi(result, "forecast_accuracy")["status"] is None
    assert result["future"]["open_order_book"] > 0 and result["future"]["suppliers_at_high_risk"] == 1


def basis_amounts(basis):
    """(stock value, cost of sales) from 'stock R... against cost of sales R... in 90 days'."""
    return D(basis.split("stock R")[1].split(" ")[0]), D(basis.split("cost of sales R")[1].split(" ")[0])


def test_status_rules():
    assert services._higher_is_better(D("95"), D("95")) == "good"
    assert services._higher_is_better(D("85"), D("95")) == "warning"
    assert services._higher_is_better(D("84.9"), D("95")) == "critical"
    assert services._higher_is_better(None, D("95")) is None


def test_the_problems_inbox(tower):
    found = services.problems(tower.company, on=ON, now=NOW)
    by_area = {}
    for problem in found:
        by_area.setdefault(problem["area"], []).append(problem)
    assert by_area["production"][0]["title"] == "Cell PRESS3 is down"
    assert by_area["service"][0]["severity"] == "critical" and "Bracket cracked" in by_area["service"][0]["detail"]
    late_order = next(p for p in by_area["sales"] if p["document"] == tower.late_so.order_number)
    assert (late_order["severity"], late_order["owner"]) == ("critical", "Sales Representative")  # 10 days late
    titles = {p["title"] for p in by_area["purchasing"]}
    assert f"Overdue delivery {tower.late_po.order_number}/10" in titles
    assert "High supply risk: Highveld Supplies CC" in titles
    assert all(p["severity"] == "critical" for p in found[:sum(p["severity"] == "critical" for p in found)])
    assert len({p["key"] for p in found}) == len(found)  # every key is unique, for decisions later (9b)
    assert services.problems(tower.company, on=ON, now=NOW) == found  # read-only: asking again changes nothing


def test_roles_see_only_their_areas(tower, client_for):
    sales_rep = client_for(Role.SALES_REP).get(f"{API}/problems/?company_code=1000&on=2026-11-15").json()
    assert sales_rep["areas"] == ["sales", "service"]
    assert {p["area"] for p in sales_rep["problems"]} <= {"sales", "service"}
    buyer = client_for(Role.PURCHASING_OFFICER)
    critical = buyer.get(f"{API}/problems/?company_code=1000&on=2026-11-15&area=purchasing&severity=critical").json()
    assert critical["count"] == critical["critical"] >= 1
    assert {p["area"] for p in critical["problems"]} == {"purchasing"}
    kpis = buyer.get(f"{API}/kpis/?company_code=1000&on=2026-11-15").json()
    assert kpi(kpis, "otif_customers")["value"] == "50.0"
    assert client_for(Role.HR_ADMIN).get(f"{API}/kpis/?company_code=1000").status_code == 403
    assert buyer.get(f"{API}/kpis/").status_code == 400  # company_code is required


def test_the_command(tower):
    out = StringIO()
    call_command("control_tower", "--company", "1000", stdout=out)
    text = out.getvalue()
    assert "On time in full, customers" in text and "Problems (" in text and "Cell PRESS3 is down" in text
