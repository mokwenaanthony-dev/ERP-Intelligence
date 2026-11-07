"""Cost and profit centres on the Universal Journal: validation, derivation and reports.

Demo set-up: profit centres PC1000 Trading and PC9000 Corporate; cost centres 1000 Management
(PC9000), 2000 Sales and 3000 Warehouse (PC1000); plant 1000 defaults to PC1000; accounts
6000/6010/6020 require a cost centre.
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.accounts.models import Role
from apps.controlling import services as controlling
from apps.controlling.models import CostCenter, PlantProfitCenter, ProfitCenter
from apps.finance import services as finance
from apps.finance.services import LineInput, PostingError
from apps.organization.models import CompanyCode, Plant

pytestmark = pytest.mark.django_db

DAY = date(2026, 5, 20)


def cc(code):
    return CostCenter.objects.get(code=code)


def pc(code):
    return ProfitCenter.objects.get(code=code)


def post(demo, *lines, **kwargs):
    return finance.post_journal_entry(company_code=demo.company, posting_date=DAY, lines=list(lines), **kwargs)


def rent(demo, amount="1000.00", **kwargs):
    return post(demo, LineInput(demo.account("6100"), "D", Decimal(amount), **kwargs),
                LineInput(demo.account("1000"), "C", Decimal(amount)))


def dims(entry):
    return [(line.gl_account.number, line.cost_center and line.cost_center.code,
             line.profit_center and line.profit_center.code) for line in entry.lines.order_by("line_number")]


def test_profit_centre_comes_from_the_cost_centre(demo):
    entry = rent(demo, cost_center=cc("1000"))
    assert dims(entry) == [("6100", "1000", "PC9000"), ("1000", None, None)]


def test_profit_centre_comes_from_the_plant(demo):
    plant = Plant.objects.get(code="1000")
    entry = post(demo, LineInput(demo.account("1000"), "D", Decimal("500")),
                 LineInput(demo.account("4000"), "C", Decimal("500"), plant=plant))
    assert dims(entry)[1] == ("4000", None, "PC1000")


def test_explicit_profit_centre_and_tax_lines(demo):
    entry = post(demo, LineInput(demo.account("6200"), "D", Decimal("100"), cost_center=cc("2000"),
                                 profit_center=pc("PC1000"), tax_code=demo.tax("I1")),
                 LineInput(demo.account("1000"), "C", Decimal("115")))
    assert dims(entry) == [("6200", "2000", "PC1000"), ("1300", None, None), ("1000", None, None)]
    balance_sheet = rent(demo, profit_center=pc("PC9000"))
    assert dims(balance_sheet)[0] == ("6100", None, "PC9000")


def test_reversal_keeps_the_dimensions(demo):
    entry = rent(demo, cost_center=cc("3000"))
    CostCenter.objects.filter(code="3000").update(is_active=False)  # later deactivated: reversal still works
    reversal = finance.reverse_journal_entry(entry)
    assert dims(reversal) == [("6100", "3000", "PC1000"), ("1000", None, None)]


def test_cost_centre_rules(demo):
    with pytest.raises(PostingError, match="needs a cost centre"):
        post(demo, LineInput(demo.account("6000"), "D", Decimal("10")),
             LineInput(demo.account("1000"), "C", Decimal("10")))
    with pytest.raises(PostingError, match="not an expense account"):
        post(demo, LineInput(demo.account("1000"), "D", Decimal("10"), cost_center=cc("1000")),
             LineInput(demo.account("3000"), "C", Decimal("10")))
    with pytest.raises(PostingError, match="belongs to profit centre PC9000, not PC1000"):
        rent(demo, cost_center=cc("1000"), profit_center=pc("PC1000"))
    CostCenter.objects.filter(code="2000").update(is_active=False)
    with pytest.raises(PostingError, match="Cost centre 2000 is inactive"):
        rent(demo, cost_center=cc("2000"))


def test_centres_must_belong_to_the_company_code(demo):
    other = CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                                       chart_of_accounts=demo.chart)
    foreign_pc = ProfitCenter.objects.create(company_code=other, code="PC2000", name="Elsewhere")
    foreign_cc = CostCenter.objects.create(company_code=other, code="9000", name="Far", profit_center=foreign_pc)
    with pytest.raises(PostingError, match="Cost centre 9000 does not belong to 1000"):
        rent(demo, cost_center=foreign_cc)
    with pytest.raises(PostingError, match="Profit centre PC2000 does not belong to 1000"):
        rent(demo, profit_center=foreign_pc)
    ProfitCenter.objects.filter(code="PC9000").update(is_active=False)
    with pytest.raises(PostingError, match="Profit centre PC9000 is inactive"):
        rent(demo, profit_center=pc("PC9000"))


def test_cost_centre_report(demo):
    rent(demo, "1000.00", cost_center=cc("1000"))
    rent(demo, "250.00", cost_center=cc("1000"))
    post(demo, LineInput(demo.account("6200"), "D", Decimal("80"), cost_center=cc("3000")),
         LineInput(demo.account("1000"), "C", Decimal("80")))
    report = controlling.cost_center_report(demo.company, 2027)
    assert [(c["cost_center"], c["total"]) for c in report["cost_centers"]] == [
        ("1000", Decimal("1250.00")), ("3000", Decimal("80.00")),
    ]
    assert report["cost_centers"][0]["accounts"] == [{"gl_account": "6100", "name": "Rent",
                                                      "amount": Decimal("1250.00")}]
    assert report["total"] == Decimal("1330.00")
    only = controlling.cost_center_report(demo.company, 2027, cost_center=cc("3000"))
    assert [c["cost_center"] for c in only["cost_centers"]] == ["3000"]
    assert controlling.cost_center_report(demo.company, 2027, 4, 12)["total"] == 0  # May is period 3


def test_profit_centre_report(demo):
    rent(demo, "1000.00", cost_center=cc("1000"))
    post(demo, LineInput(demo.account("1000"), "D", Decimal("500")),
         LineInput(demo.account("4000"), "C", Decimal("500"), plant=Plant.objects.get(code="1000")))
    report = controlling.profit_center_report(demo.company, 2027)
    assert [(c["profit_center"], c["revenue"], c["expenses"], c["result"]) for c in report["profit_centers"]] == [
        ("PC1000", Decimal("500.00"), Decimal("0.00"), Decimal("500.00")),
        ("PC9000", Decimal("0.00"), Decimal("1000.00"), Decimal("-1000.00")),
        (None, Decimal("10000.00"), Decimal("0.00"), Decimal("10000.00")),  # demo DR00000001 has no plant
    ]
    assert report["profit_centers"][2]["name"] == "Unassigned"
    assert report["totals"] == {"revenue": Decimal("10500.00"), "expenses": Decimal("1000.00"),
                                "result": Decimal("9500.00")}


def test_strings(demo):
    assert str(cc("1000")) == "1000 Management"
    assert str(pc("PC1000")) == "PC1000 Trading"
    assert str(PlantProfitCenter.objects.get()) == "Plant 1000 -> PC1000"


# --- API --------------------------------------------------------------------------------------

def test_master_data_api(demo, client_for):
    manager = client_for(Role.FINANCE_MANAGER)
    created = manager.post("/api/controlling/profit-centers/", {"company_code": "1000", "code": "PC2000",
                                                                "name": "Services"}, format="json")
    assert created.status_code == 201, created.data
    center = manager.post("/api/controlling/cost-centers/", {"company_code": "1000", "code": "4000", "name": "IT",
                                                             "category": "SERV", "profit_center": "PC2000"},
                          format="json")
    assert center.status_code == 201, center.data
    assert center.data["profit_center"] == "PC2000"
    missing = manager.post("/api/controlling/cost-centers/", {"company_code": "1000", "code": "4100", "name": "X",
                                                              "profit_center": "NOPE"}, format="json")
    assert "profit_center" in missing.data
    moved = manager.patch(f"/api/controlling/cost-centers/{center.data['id']}/", {"profit_center": "PC9000"},
                          format="json")
    assert moved.data["profit_center"] == "PC9000"
    assert manager.get("/api/controlling/cost-centers/", {"company_code": "1000"}).data["count"] == 4
    assert client_for(Role.VIEWER).get("/api/controlling/cost-centers/").status_code == 200
    assert client_for(Role.ACCOUNTANT).post("/api/controlling/profit-centers/", {}, format="json").status_code == 403


def test_cost_centre_cannot_move_company_without_a_profit_centre(demo, client_for):
    manager = client_for(Role.FINANCE_MANAGER)
    other = CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                                       chart_of_accounts=demo.chart)
    response = manager.patch(f"/api/controlling/cost-centers/{cc('3000').pk}/", {"company_code": other.code},
                             format="json")
    assert response.status_code == 400
    assert "profit_center" in response.data


def test_used_centres_cannot_be_deleted(demo, client_for):
    rent(demo, cost_center=cc("1000"))
    manager = client_for(Role.FINANCE_MANAGER)
    assert manager.delete(f"/api/controlling/cost-centers/{cc('1000').pk}/").status_code == 409


def test_plant_assignment_api(demo, client_for):
    manager = client_for(Role.FINANCE_MANAGER)
    durban = Plant.objects.create(code="1100", name="Durban", company_code=demo.company)
    ok = manager.post("/api/controlling/plant-profit-centers/", {"plant": "1100", "profit_center": pc("PC9000").pk},
                      format="json")
    assert ok.status_code == 201, ok.data
    other = CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                                       chart_of_accounts=demo.chart)
    foreign = ProfitCenter.objects.create(company_code=other, code="PC2000", name="Elsewhere")
    PlantProfitCenter.objects.filter(plant=durban).delete()
    bad = manager.post("/api/controlling/plant-profit-centers/", {"plant": "1100", "profit_center": foreign.pk},
                       format="json")
    assert bad.status_code == 400


def test_journal_api_takes_centres_by_code(demo, client_for):
    accountant = client_for(Role.ACCOUNTANT)
    payload = {"company_code": "1000", "posting_date": "2026-05-20", "lines": [
        {"gl_account": "6100", "debit_credit": "D", "amount": "300.00", "cost_center": "2000"},
        {"gl_account": "1000", "debit_credit": "C", "amount": "300.00"},
    ]}
    response = accountant.post("/api/finance/journal-entries/", payload, format="json")
    assert response.status_code == 201, response.data
    assert (response.data["lines"][0]["cost_center"], response.data["lines"][0]["profit_center"]) == (
        "2000", "PC1000",
    )
    payload["lines"][0].update({"cost_center": "ZZZ", "profit_center": "ZZZ"})
    bad = accountant.post("/api/finance/journal-entries/", payload, format="json")
    assert set(bad.data["lines"][0]) == {"cost_center", "profit_center"}


def test_report_endpoints(demo, client_for):
    rent(demo, cost_center=cc("1000"))
    accountant = client_for(Role.ACCOUNTANT)
    co_url, pc_url = "/api/controlling/reports/cost-centers/", "/api/controlling/reports/profit-centers/"
    report = accountant.get(co_url, {"company_code": "1000", "fiscal_year": 2027, "cost_center": "1000"})
    assert report.data["total"] == Decimal("1000.00")
    assert accountant.get("/api/controlling/reports/cost-centers/", {
        "company_code": "1000", "fiscal_year": 2027, "cost_center": "NOPE"}).status_code == 400
    assert accountant.get("/api/controlling/reports/cost-centers/", {"company_code": "1000"}).status_code == 400
    profit = accountant.get(pc_url, {"company_code": "1000", "fiscal_year": 2027, "period_from": 3, "period_to": 3})
    assert profit.data["totals"]["expenses"] == Decimal("1000.00")
    assert client_for(Role.HR_ADMIN).get("/api/controlling/reports/profit-centers/").status_code == 403
