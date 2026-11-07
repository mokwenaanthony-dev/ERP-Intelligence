"""South African payroll: PAYE, UIF and SDL, and its posting to the journal by cost centre.

Demo employees (hired 2024-03-01), tax year 2027 tables (rebate R17,820):
    PN00000001 Thandi Nkosi    Managing director     cost centre 1000  R85,000
    PN00000002 Sipho Dlamini   Sales representative  cost centre 2000  R32,000
    PN00000003 Lerato Mokoena  Warehouse supervisor  cost centre 3000  R18,500
All three earn above the UIF ceiling (R17,712), so UIF is R177.12 each for employee and employer.
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.controlling import services as controlling
from apps.finance import services as finance
from apps.hr import services as hr
from apps.hr.models import Employee, OrgUnit, PayeTable, PayrollResult, PayrollRun, PayrollSetting, Position
from apps.hr.services import PayrollError

pytestmark = pytest.mark.django_db


def run_may(demo, **kwargs):
    return hr.post_payroll_run(demo.company, year=2026, month=5, **kwargs)


def table():
    return PayeTable.objects.get(tax_year=2027)


@pytest.mark.parametrize("day, year", [(date(2026, 3, 1), 2027), (date(2027, 2, 28), 2027), (date(2026, 2, 28), 2026)])
def test_tax_year(day, year):
    assert hr.tax_year_for(day) == year


@pytest.mark.parametrize("salary, paye", [
    ("18500.00", "1845.00"),     # 222,000 x 18% - 17,820 = 22,140 / 12
    ("32000.00", "5204.75"),     # 79,998 + 31% x 900 - 17,820 = 62,457 / 12
    ("85000.00", "24707.75"),    # 259,783 + 41% x 133,000 - 17,820 = 296,493 / 12
    ("1000.00", "0.00"),         # below the tax threshold
    ("0.00", "0.00"),
])
def test_monthly_paye(demo, salary, paye):
    assert hr.monthly_paye(table(), Decimal(salary)) == Decimal(paye)


def test_uif_is_capped_and_sdl_is_on_full_gross(demo):
    setting = PayrollSetting.objects.get()
    md = Employee.objects.get(personnel_number="PN00000001")
    result = hr.calculate(md, setting, table())
    assert result == {"gross": Decimal("85000.00"), "paye": Decimal("24707.75"), "uif_employee": Decimal("177.12"),
                      "uif_employer": Decimal("177.12"), "sdl": Decimal("850.00"), "net": Decimal("60115.13")}


def test_payroll_run_posts_by_cost_centre(demo):
    run = run_may(demo)
    assert (run.run_number, run.tax_year, run.posting_date, run.status) == ("PR00000001", 2027, date(2026, 5, 31),
                                                                            "POSTED")
    assert (run.gross, run.paye, run.uif_employee, run.uif_employer, run.sdl, run.net) == (
        Decimal("135500.00"), Decimal("31757.50"), Decimal("531.36"), Decimal("531.36"), Decimal("1355.00"),
        Decimal("103211.14"),
    )
    entry = run.journal_entry
    assert (entry.document_number, entry.document_type, entry.reference) == ("PY00000001", "PY", "PR00000001")
    assert [(line.gl_account.number, line.amount, line.cost_center and line.cost_center.code,
             line.profit_center and line.profit_center.code) for line in entry.lines.order_by("line_number")] == [
        ("6000", Decimal("85000.00"), "1000", "PC9000"),
        ("6010", Decimal("177.12"), "1000", "PC9000"),
        ("6020", Decimal("850.00"), "1000", "PC9000"),
        ("6000", Decimal("32000.00"), "2000", "PC1000"),
        ("6010", Decimal("177.12"), "2000", "PC1000"),
        ("6020", Decimal("320.00"), "2000", "PC1000"),
        ("6000", Decimal("18500.00"), "3000", "PC1000"),
        ("6010", Decimal("177.12"), "3000", "PC1000"),
        ("6020", Decimal("185.00"), "3000", "PC1000"),
        ("2400", Decimal("-31757.50"), None, None),
        ("2410", Decimal("-1062.72"), None, None),
        ("2420", Decimal("-1355.00"), None, None),
        ("2500", Decimal("-103211.14"), None, None),
    ]
    assert [(r.employee.personnel_number, r.net) for r in run.results.all()] == [
        ("PN00000001", Decimal("60115.13")), ("PN00000002", Decimal("26618.13")), ("PN00000003", Decimal("16477.88")),
    ]
    assert finance.trial_balance(demo.company, 2027)["balanced"] is True
    costs = controlling.cost_center_report(demo.company, 2027)
    assert [(c["cost_center"], c["total"]) for c in costs["cost_centers"]] == [
        ("1000", Decimal("86027.12")), ("2000", Decimal("32497.12")), ("3000", Decimal("18862.12")),
    ]
    profit = controlling.profit_center_report(demo.company, 2027)
    assert [(c["profit_center"], c["expenses"]) for c in profit["profit_centers"]][:2] == [
        ("PC1000", Decimal("51359.24")), ("PC9000", Decimal("86027.12")),
    ]
    assert str(run) == "PR00000001 2026-05 (POSTED)"
    assert str(run.results.first()) == "PR00000001: PN00000001 net 60115.13"


def test_one_payroll_per_month_until_reversed(demo):
    run = run_may(demo, posting_date=date(2026, 5, 25))
    with pytest.raises(PayrollError, match="already posted"):
        run_may(demo)
    reversed_run = hr.reverse_payroll_run(run)
    assert reversed_run.status == "REVERSED"
    assert finance.trial_balance(demo.company, 2027)["balanced"] is True
    with pytest.raises(PayrollError, match="already reversed"):
        hr.reverse_payroll_run(run)
    assert run_may(demo).run_number == "PR00000002"


def test_who_is_paid(demo):
    Employee.objects.filter(personnel_number="PN00000003").update(termination_date=date(2026, 4, 30))
    position = Position.objects.get(code="SREP")
    hr.create_employee(company_code=demo.company, position=position, first_name="Late", last_name="Starter",
                       hire_date=date(2026, 6, 1), monthly_salary=Decimal("20000"))
    hr.create_employee(company_code=demo.company, position=position, first_name="Last", last_name="Day",
                       hire_date=date(2026, 3, 1), termination_date=date(2026, 5, 1), monthly_salary=Decimal("10000"))
    run = run_may(demo)
    assert [r.employee.first_name for r in run.results.all()] == ["Thandi", "Sipho", "Last"]


def test_position_cost_centre_overrides_the_org_unit(demo):
    from apps.controlling.models import CostCenter

    Position.objects.filter(code="SREP").update(cost_center=CostCenter.objects.get(code="1000"))
    run = run_may(demo)
    assert PayrollResult.objects.get(run=run, employee__first_name="Sipho").cost_center.code == "1000"


def test_payroll_rules(demo):
    with pytest.raises(PayrollError, match="between 1 and 12"):
        hr.post_payroll_run(demo.company, year=2026, month=13)
    with pytest.raises(PayrollError, match="no PAYE table for tax year 2028"):
        hr.post_payroll_run(demo.company, year=2027, month=3)
    Employee.objects.update(hire_date=date(2026, 6, 1))
    with pytest.raises(PayrollError, match="Nobody in company code 1000 is employed in 2026-05"):
        run_may(demo)


def test_missing_cost_centre_and_settings(demo):
    OrgUnit.objects.filter(code="WH").update(cost_center=None)
    with pytest.raises(PayrollError, match="PN00000003 has no cost centre"):
        run_may(demo)
    PayrollSetting.objects.all().delete()
    with pytest.raises(PayrollError, match="not set up"):
        run_may(demo)
    assert PayrollRun.objects.count() == 0


def test_no_sdl_when_exempt(demo):
    PayrollSetting.objects.update(sdl_rate=Decimal("0"))
    run = run_may(demo)
    assert run.sdl == 0
    assert not run.journal_entry.lines.filter(gl_account__number__in=["6020", "2420"]).exists()


def test_hiring_rules(demo):
    from apps.organization.models import CompanyCode

    other = CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                                       chart_of_accounts=demo.chart)
    position = Position.objects.get(code="MD")
    with pytest.raises(PayrollError, match="not in company code 2000"):
        hr.create_employee(company_code=other, position=position, first_name="A", last_name="B",
                           hire_date=date(2026, 1, 1), monthly_salary=Decimal("1"))
    with pytest.raises(PayrollError, match="cannot be negative"):
        hr.create_employee(company_code=demo.company, position=position, first_name="A", last_name="B",
                           hire_date=date(2026, 1, 1), monthly_salary=Decimal("-1"))


def test_strings(demo):
    employee = Employee.objects.get(personnel_number="PN00000001")
    assert str(employee) == "PN00000001 Thandi Nkosi"
    assert str(employee.position) == "MD Managing director"
    assert str(employee.position.org_unit) == "EXEC Executive"
    assert str(table()) == "Tax year 2027 (rebate 17820.00)"
    assert str(table().brackets.last()) == "2027: above 1878600.00 -> 666339.00 + 45.00%"
    assert str(PayrollSetting.objects.get()) == "Payroll settings 1000"


# --- API --------------------------------------------------------------------------------------

def test_payroll_api(demo, client_for):
    hr_admin = client_for(Role.HR_ADMIN)
    created = hr_admin.post("/api/hr/payroll-runs/", {"company_code": "1000", "year": 2026, "month": 5},
                            format="json")
    assert created.status_code == 201, created.data
    assert (created.data["run_number"], created.data["journal_entry"], created.data["net"]) == (
        "PR00000001", "PY00000001", "103211.14",
    )
    assert created.data["results"][0] == {
        "personnel_number": "PN00000001", "name": "Thandi Nkosi", "cost_center": "1000", "gross": "85000.00",
        "paye": "24707.75", "uif_employee": "177.12", "uif_employer": "177.12", "sdl": "850.00", "net": "60115.13",
    }
    again = hr_admin.post("/api/hr/payroll-runs/", {"company_code": "1000", "year": 2026, "month": 5}, format="json")
    assert "already posted" in str(again.data)
    accountant = client_for(Role.ACCOUNTANT)
    assert accountant.get("/api/hr/payroll-runs/", {"year": 2026, "status": "POSTED"}).data["count"] == 1
    assert accountant.post(f"/api/hr/payroll-runs/{created.data['id']}/reverse/", {}, format="json").status_code == 403
    manager = client_for(Role.FINANCE_MANAGER)
    reversed_ = manager.post(f"/api/hr/payroll-runs/{created.data['id']}/reverse/", {"posting_date": "2026-05-31"},
                             format="json")
    assert (reversed_.status_code, reversed_.data["status"]) == (200, "REVERSED")
    assert manager.post(f"/api/hr/payroll-runs/{created.data['id']}/reverse/", {}, format="json").status_code == 400
