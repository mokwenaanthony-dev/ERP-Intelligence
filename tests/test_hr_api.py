"""HR REST API: org units, positions and employees (HR admin only)."""
import pytest

from apps.accounts.models import Role
from apps.hr.models import Employee, OrgUnit, Position

pytestmark = pytest.mark.django_db


@pytest.fixture
def hr_admin(client_for):
    return client_for(Role.HR_ADMIN)


def test_org_chart_and_hiring(demo, hr_admin):
    unit = hr_admin.post("/api/hr/org-units/", {"company_code": "1000", "code": "FIN", "name": "Finance",
                                                "parent": OrgUnit.objects.get(code="EXEC").pk,
                                                "cost_center": "1000"}, format="json")
    assert unit.status_code == 201, unit.data
    assert unit.data["cost_center"] == "1000"
    position = hr_admin.post("/api/hr/positions/", {"org_unit": unit.data["id"], "code": "ACC", "title": "Accountant"},
                             format="json")
    assert position.status_code == 201, position.data
    assert (position.data["cost_center"], position.data["effective_cost_center"]) == (None, "1000")

    hired = hr_admin.post("/api/hr/employees/", {
        "company_code": "1000", "first_name": "Naledi", "last_name": "Khumalo", "position": position.data["id"],
        "hire_date": "2026-06-01", "monthly_salary": "41000.00", "email": "naledi@example.com",
    }, format="json")
    assert hired.status_code == 201, hired.data
    assert (hired.data["personnel_number"], hired.data["cost_center"]) == ("PN00000004", "1000")
    assert Employee.objects.get(personnel_number="PN00000004").created_by == hr_admin.user

    raise_ = hr_admin.patch(f"/api/hr/employees/{hired.data['id']}/", {"monthly_salary": "43000.00"}, format="json")
    assert raise_.data["monthly_salary"] == "43000.00"
    leaving = hr_admin.patch(f"/api/hr/employees/{hired.data['id']}/", {"termination_date": "2026-05-01"},
                             format="json")
    assert "termination_date" in leaving.data
    assert hr_admin.get("/api/hr/employees/", {"company_code": "1000", "org_unit": "FIN"}).data["count"] == 1
    assert hr_admin.get("/api/hr/employees/", {"active": "true"}).data["count"] == 4


def test_org_unit_rules(demo, hr_admin):
    from apps.organization.models import CompanyCode

    bad_cc = hr_admin.post("/api/hr/org-units/", {"company_code": "1000", "code": "X", "name": "X",
                                                  "cost_center": "NOPE"}, format="json")
    assert "cost_center" in bad_cc.data
    exec_unit = OrgUnit.objects.get(code="EXEC")
    sales_unit = OrgUnit.objects.get(code="SALES")
    hr_admin.patch(f"/api/hr/org-units/{sales_unit.pk}/", {"parent": exec_unit.pk}, format="json")
    loop = hr_admin.patch(f"/api/hr/org-units/{exec_unit.pk}/", {"parent": sales_unit.pk}, format="json")
    assert loop.status_code == 400
    assert "below itself" in str(loop.data)
    CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                               chart_of_accounts=demo.chart)
    foreign = hr_admin.post("/api/hr/org-units/", {"company_code": "2000", "code": "Y", "name": "Y",
                                                   "parent": exec_unit.pk}, format="json")
    assert "another company code" in str(foreign.data)
    cleared = hr_admin.patch(f"/api/hr/org-units/{sales_unit.pk}/", {"cost_center": None}, format="json")
    assert cleared.data["cost_center"] is None


def test_position_and_employee_rules(demo, hr_admin):
    from apps.organization.models import CompanyCode

    unit = OrgUnit.objects.get(code="SALES")
    override = hr_admin.post("/api/hr/positions/", {"org_unit": unit.pk, "code": "KAM", "title": "Key accounts",
                                                    "cost_center": "1000"}, format="json")
    assert override.data["effective_cost_center"] == "1000"
    assert "cost_center" in hr_admin.post("/api/hr/positions/", {"org_unit": unit.pk, "code": "Z", "title": "Z",
                                                                 "cost_center": "NOPE"}, format="json").data
    CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                               chart_of_accounts=demo.chart)
    wrong = hr_admin.post("/api/hr/employees/", {
        "company_code": "2000", "first_name": "A", "last_name": "B", "position": Position.objects.get(code="MD").pk,
        "hire_date": "2026-06-01", "monthly_salary": "1000.00",
    }, format="json")
    assert "position" in wrong.data
    negative = hr_admin.post("/api/hr/employees/", {
        "company_code": "1000", "first_name": "A", "last_name": "B", "position": Position.objects.get(code="MD").pk,
        "hire_date": "2026-06-01", "monthly_salary": "-1",
    }, format="json")
    assert "monthly_salary" in negative.data


def test_paid_employees_cannot_be_deleted(demo, hr_admin, client_for):
    client_for(Role.FINANCE_MANAGER).post("/api/hr/payroll-runs/", {"company_code": "1000", "year": 2026, "month": 5},
                                          format="json")
    employee = Employee.objects.get(personnel_number="PN00000001")
    assert hr_admin.delete(f"/api/hr/employees/{employee.pk}/").status_code == 409


def test_only_hr_sees_people(demo, client_for):
    for role in (Role.FINANCE_MANAGER, Role.ACCOUNTANT, Role.SALES_REP, Role.VIEWER):
        assert client_for(role).get("/api/hr/employees/").status_code == 403, role
