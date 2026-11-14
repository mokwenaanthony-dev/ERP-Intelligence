"""SRM (Wave 7b): onboarding, requests for quotation, contracts and supplier evaluation.

Every status change is repeated to prove it is idempotent (ADR 0010); the applicant's contact details
are checked to stay out of the log, other roles' API answers and the knowledge graph (ADR 0013); the
evaluation is checked against goods receipts and invoices posted through the purchasing services.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role
from apps.graph import processor
from apps.graph.store import MemoryStore
from apps.masterdata.models import BusinessPartner, Material, SupplierRole
from apps.organization.models import PurchasingOrganization
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder
from apps.srm import services
from apps.srm.models import Contract, RequestForQuotation, SupplierApplication, SupplierEvaluation, SupplierQuote
from apps.srm.services import ContractItemInput, RFQItemInput, SRMError

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/srm"
DAY = date(2026, 5, 4)  # inside demo fiscal year 2027 (1 March 2026 - 28 February 2027)


@pytest.fixture
def srm(demo):
    demo.org = PurchasingOrganization.objects.get(code="1000")
    demo.widget = Material.objects.get(number="TG-1001")
    demo.gadget = Material.objects.get(number="TG-1002")
    demo.second = services.approve_application(application(demo)).business_partner
    return demo


def application(srm, **extra):
    values = {"company_name": "Gauteng Components (Pty) Ltd", "registration_number": "2019/123456/07",
              "vat_number": "4555555555", "bbbee_level": 2, "contact_name": "Demo Sales",
              "email": "sales@gautengcomponents.example.co.za", "phone": "+27 11 555 0200", **extra}
    return services.submit_application(company_code=srm.company, **values)


def sent_rfq(srm):
    rfq = services.create_rfq(purchasing_organization=srm.org, title="Winter stock", deadline=DAY + timedelta(days=7),
                              suppliers=[srm.supplier, srm.second],
                              items=[RFQItemInput(srm.widget, D("50")), RFQItemInput(srm.gadget, D("20"))])
    return services.send_rfq(rfq)


def quote_of(rfq, supplier):
    return rfq.quotes.get(supplier=supplier)


def quoted_rfq(srm):
    rfq = sent_rfq(srm)
    services.submit_quote(quote_of(rfq, srm.supplier), prices={srm.widget: D("185"), srm.gadget: D("460")},
                          delivery_days=5, submitted_on=DAY)
    services.submit_quote(quote_of(rfq, srm.second), prices={srm.widget: D("178"), srm.gadget: D("455")},
                          delivery_days=7, submitted_on=DAY + timedelta(days=1), supplier_reference="GC-14")
    return rfq


# --- onboarding ------------------------------------------------------------------------------------

def test_an_approved_application_becomes_a_supplier(srm):
    approved = SupplierApplication.objects.get(business_partner=srm.second)
    assert (approved.application_number, approved.status) == ("OB00000001", "APPROVED")
    assert (srm.second.name, srm.second.vat_number) == ("Gauteng Components (Pty) Ltd", "4555555555")
    role = SupplierRole.objects.get(business_partner=srm.second, company_code=srm.company)
    assert role.reconciliation_account.number == "2000"  # payables: the supplier can be invoiced at once
    assert services.approve_application(approved) == approved  # again: unchanged, no second partner
    assert BusinessPartner.objects.filter(vat_number="4555555555").count() == 1


def test_an_existing_partner_with_the_same_vat_number_is_reused(srm):
    again = services.approve_application(application(srm, company_name="Gauteng Components"))
    assert again.business_partner == srm.second and again.application_number == "OB00000002"


def test_due_diligence_and_rejection(srm):
    incomplete = application(srm, company_name="Quick Deals", registration_number="", vat_number="")
    with pytest.raises(SRMError, match="without a registration number and a VAT number"):
        services.approve_application(incomplete)
    with pytest.raises(SRMError, match="1 to 8"):
        application(srm, bbbee_level=9)
    rejected = services.reject_application(incomplete, "No registration or VAT number")
    assert services.reject_application(rejected, "No registration or VAT number").status == "REJECTED"  # again
    with pytest.raises(SRMError, match="is REJECTED"):
        services.approve_application(rejected)
    with pytest.raises(SRMError, match="Say why"):
        services.reject_application(application(srm), " ")


# --- requests for quotation ------------------------------------------------------------------------------

def test_rfq_rules(srm):
    with pytest.raises(SRMError, match="at least two different suppliers"):
        services.create_rfq(purchasing_organization=srm.org, title="x", deadline=DAY,
                            suppliers=[srm.supplier, srm.supplier], items=[RFQItemInput(srm.widget, D("1"))])
    with pytest.raises(SRMError, match="only once"):
        services.create_rfq(purchasing_organization=srm.org, title="x", deadline=DAY,
                            suppliers=[srm.supplier, srm.second],
                            items=[RFQItemInput(srm.widget, D("1")), RFQItemInput(srm.widget, D("2"))])
    with pytest.raises(SRMError, match="is not a supplier"):
        services.create_rfq(purchasing_organization=srm.org, title="x", deadline=DAY,
                            suppliers=[srm.supplier, srm.customer], items=[RFQItemInput(srm.widget, D("1"))])
    rfq = sent_rfq(srm)
    assert rfq.rfq_number == "RQ00000001" and services.send_rfq(rfq).status == "SENT"  # again: unchanged
    quote = quote_of(rfq, srm.supplier)
    with pytest.raises(SRMError, match="every item"):
        services.submit_quote(quote, prices={srm.widget: D("185")}, delivery_days=5, submitted_on=DAY)
    with pytest.raises(SRMError, match="deadline"):
        services.submit_quote(quote, prices={srm.widget: D("185"), srm.gadget: D("460")}, delivery_days=5,
                              submitted_on=DAY + timedelta(days=8))
    with pytest.raises(SRMError, match="has not quoted"):
        services.award_rfq(rfq, quote, on=DAY)


def test_a_quote_repeated_is_a_no_op_and_a_revision_replaces_it(srm):
    rfq = quoted_rfq(srm)
    quote = quote_of(rfq, srm.supplier)
    stamp = quote.updated_at
    services.submit_quote(quote, prices={srm.widget: D("185"), srm.gadget: D("460")}, delivery_days=5,
                          submitted_on=DAY)
    quote.refresh_from_db()
    assert quote.updated_at == stamp  # the same quote again: nothing written
    services.submit_quote(quote, prices={srm.widget: D("175"), srm.gadget: D("460")}, delivery_days=5,
                          submitted_on=DAY + timedelta(days=2))
    assert services.quote_total(quote) == D("17950.00")  # 50 x 175 + 20 x 460
    assert quote.items.count() == 2


def test_comparison_and_award_create_the_purchase_order(srm):
    rfq = quoted_rfq(srm)
    comparison = services.compare_quotes(rfq)
    assert [row["supplier"] for row in comparison["ranking"]] == [srm.second.number, srm.supplier.number]
    assert comparison["ranking"][0]["total"] == D("18000.00")  # 50 x 178 + 20 x 455
    assert {i["material"]: (i["lowest_price"], i["lowest_from"]) for i in comparison["items"]} == {
        "TG-1001": (D("178.00"), srm.second.number), "TG-1002": (D("455.00"), srm.second.number)}

    awarded = services.award_rfq(rfq, quote_of(rfq, srm.second), on=DAY + timedelta(days=8))
    order = awarded.purchase_order
    assert (awarded.status, order.supplier, order.supplier_reference) == ("AWARDED", srm.second, "GC-14")
    assert order.delivery_date == DAY + timedelta(days=15)  # order date + 7 delivery days
    assert {i.material.number: i.net_price for i in order.items.all()} == {"TG-1001": D("178.00"),
                                                                           "TG-1002": D("455.00")}
    assert quote_of(rfq, srm.supplier).status == "LOST" and quote_of(rfq, srm.second).status == "AWARDED"
    # Awarding again (even to the other supplier) returns the awarded request: still one order.
    again = services.award_rfq(rfq, quote_of(rfq, srm.supplier), on=DAY + timedelta(days=9))
    assert again.purchase_order == order and PurchaseOrder.objects.filter(supplier=srm.supplier).count() == 0
    with pytest.raises(SRMError, match="cancel the purchase order instead"):
        services.cancel_rfq(rfq)
    with pytest.raises(SRMError, match="does not take quotes"):
        services.submit_quote(quote_of(rfq, srm.supplier), prices={srm.widget: 1, srm.gadget: 1}, delivery_days=1,
                              submitted_on=DAY)


def test_a_cancelled_rfq_stays_cancelled(srm):
    rfq = sent_rfq(srm)
    assert services.cancel_rfq(rfq).status == "CANCELLED"
    assert services.cancel_rfq(rfq).status == "CANCELLED"  # again: unchanged
    with pytest.raises(SRMError, match="Only a SENT request can be awarded"):
        services.award_rfq(rfq, quote_of(rfq, srm.supplier), on=DAY)


# --- contracts ---------------------------------------------------------------------------------------

def contract_with(srm, target="100"):
    return services.create_contract(purchasing_organization=srm.org, supplier=srm.supplier,
                                    valid_from=date(2026, 4, 1), valid_to=date(2026, 9, 30),
                                    items=[ContractItemInput(srm.widget, D("180"), D(target))])


def test_releases_use_the_contract_price_up_to_the_target(srm):
    contract = contract_with(srm)
    assert contract.contract_number == "CT00000001"
    first = services.release_order(contract, order_date=DAY, items=[(srm.widget, D("60"))])
    assert (first.supplier, first.supplier_reference) == (srm.supplier, "CT00000001")
    assert first.items.get().net_price == D("180.00")
    with pytest.raises(SRMError, match="Only 40.000 of TG-1001 is left"):
        services.release_order(contract, order_date=DAY, items=[(srm.widget, D("41"))])
    purchasing.cancel_order(first)  # a cancelled release gives its quantity back
    assert services.released_quantities(contract) == {}
    services.release_order(contract, order_date=DAY, items=[(srm.widget, D("100"))])
    with pytest.raises(SRMError, match="not on contract"):
        services.release_order(contract, order_date=DAY, items=[(srm.gadget, D("1"))])
    with pytest.raises(SRMError, match="valid from 2026-04-01 to 2026-09-30"):
        services.release_order(contract, order_date=date(2026, 10, 1), items=[(srm.widget, D("1"))])


def test_a_cancelled_contract_takes_no_releases(srm):
    contract = contract_with(srm)
    assert services.cancel_contract(contract).status == "CANCELLED"
    assert services.cancel_contract(contract).status == "CANCELLED"  # again: unchanged
    with pytest.raises(SRMError, match="is CANCELLED"):
        services.release_order(contract, order_date=DAY, items=[(srm.widget, D("1"))])
    with pytest.raises(SRMError, match="cannot end before"):
        services.create_contract(purchasing_organization=srm.org, supplier=srm.supplier, valid_from=DAY,
                                 valid_to=DAY - timedelta(days=1), items=[ContractItemInput(srm.widget, 1, 1)])


# --- evaluation --------------------------------------------------------------------------------------

def received(order, on, quantity=None):
    order = purchasing.release_order(order)
    items = None if quantity is None else [purchasing.ReceiptItemInput(order.items.get(), D(quantity))]
    purchasing.post_goods_receipt(order, receipt_date=on, items=items)
    return order


def test_evaluation_measures_receipts_and_invoices(srm):
    contract = contract_with(srm, target="500")
    # Order 1: due 10 May, received in full on 8 May (on time), invoiced at the order price.
    on_time = received(services.release_order(contract, order_date=DAY, items=[(srm.widget, D("100"))],
                                              delivery_date=date(2026, 5, 10)), date(2026, 5, 8))
    purchasing.post_supplier_invoice(on_time, invoice_date=date(2026, 5, 9), supplier_reference="HV-1")
    # Order 2: due 10 May, half received on 15 May (late), invoiced 3 % above the order price.
    late = received(services.release_order(contract, order_date=DAY, items=[(srm.widget, D("100"))],
                                           delivery_date=date(2026, 5, 10)), date(2026, 5, 15), quantity="50")
    purchasing.post_supplier_invoice(late, invoice_date=date(2026, 5, 16), supplier_reference="HV-2", items=[
        purchasing.InvoiceItemInput(late.items.get(), D("50"), net_price=D("185.40"))])

    measured = services.measure_supplier(srm.company, srm.supplier, date(2026, 5, 1), date(2026, 5, 31))
    assert (measured["on_time_rate"], measured["in_full_rate"], measured["price_rate"]) == (
        D("50.0"), D("75.0"), D("50.0"))
    evaluation = services.evaluate_supplier(company_code=srm.company, supplier=srm.supplier,
                                            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                                            quality_score=4, evaluated_on=date(2026, 6, 1))
    # 0.4 x 50 + 0.2 x 75 + 0.2 x 50 + 0.2 x 75 (quality 4 -> 75 %) = 60.0
    assert (evaluation.overall_score, evaluation.grade) == (D("60.0"), "C")
    again = services.evaluate_supplier(company_code=srm.company, supplier=srm.supplier,
                                       period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                                       quality_score=4, evaluated_on=date(2026, 6, 2))
    assert again.pk == evaluation.pk and SupplierEvaluation.objects.count() == 1  # one row per period


def test_measures_without_data_are_left_out_and_grades(srm):
    evaluation = services.evaluate_supplier(company_code=srm.company, supplier=srm.second,
                                            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                                            quality_score=5, evaluated_on=date(2026, 6, 1))
    assert (evaluation.on_time_rate, evaluation.price_rate) == (None, None)
    assert (evaluation.overall_score, evaluation.grade) == (D("100.0"), "A")  # only quality was measurable
    assert [services.grade_for(D(s)) for s in ("90", "89.9", "75", "60", "59.9")] == ["A", "B", "B", "C", "D"]
    with pytest.raises(SRMError, match="1 \\(poor\\) to 5"):
        services.evaluate_supplier(company_code=srm.company, supplier=srm.second, period_start=DAY,
                                   period_end=DAY, quality_score=0, evaluated_on=DAY)
    rows = {row["supplier"]: row for row in services.scorecard(srm.company)}
    assert rows[srm.second.number]["grade"] == "A" and rows[srm.supplier.number]["grade"] is None


# --- API -------------------------------------------------------------------------------------------

def test_purchasing_runs_the_whole_path_through_the_api(srm, client_for):
    buyer = client_for(Role.PURCHASING_OFFICER)
    applied = buyer.post(f"{API}/applications/", {"company_code": "1000", "company_name": "Karoo Fasteners CC",
                                                  "registration_number": "2020/555/23", "vat_number": "4777777777",
                                                  "bbbee_level": 1}, format="json")
    assert applied.status_code == 201, applied.data
    approved = buyer.post(f"{API}/applications/{applied.data['id']}/approve/")
    assert approved.status_code == 200 and approved.data["status"] == "APPROVED"
    newcomer = approved.data["business_partner"]

    rfq = buyer.post(f"{API}/rfqs/", {"purchasing_organization": "1000", "title": "Widgets",
                                      "deadline": "2026-05-11", "suppliers": [srm.supplier.number, newcomer],
                                      "items": [{"material": "TG-1001", "quantity": "50"}]}, format="json")
    assert rfq.status_code == 201, rfq.data
    url = f"{API}/rfqs/{rfq.data['id']}"
    assert buyer.post(f"{url}/send/").data["status"] == "SENT"
    for supplier, price in ((srm.supplier.number, "185.00"), (newcomer, "181.00")):
        answer = buyer.post(f"{url}/quote/", {"supplier": supplier, "prices": {"TG-1001": price},
                                              "delivery_days": 5, "submitted_on": "2026-05-05"}, format="json")
        assert answer.status_code == 200, answer.data
    comparison = buyer.get(f"{url}/comparison/").json()
    assert comparison["ranking"][0]["supplier"] == newcomer and comparison["ranking"][0]["total"] == "9050.00"
    awarded = buyer.post(f"{url}/award/", {"supplier": newcomer, "on": "2026-05-12"}, format="json")
    assert awarded.status_code == 200 and awarded.data["purchase_order"] == "PO00000001"

    contract = buyer.post(f"{API}/contracts/", {"purchasing_organization": "1000", "supplier": srm.supplier.number,
                                                "valid_from": "2026-04-01", "valid_to": "2026-09-30",
                                                "items": [{"material": "TG-1002", "net_price": "450.00",
                                                           "target_quantity": "30"}]}, format="json")
    assert contract.status_code == 201, contract.data
    release = buyer.post(f"{API}/contracts/{contract.data['id']}/release/",
                         {"order_date": "2026-05-12", "items": [{"material": "TG-1002", "quantity": "10"}]},
                         format="json")
    assert release.status_code == 201 and release.json() == {"purchase_order": "PO00000002",
                                                             "contract": "CT00000001", "net_total": "4500.00"}
    item = buyer.get(f"{API}/contracts/{contract.data['id']}/").json()["items"][0]
    assert (item["released_quantity"], item["open_quantity"]) == ("10.000", "20.000")

    evaluated = buyer.post(f"{API}/evaluations/", {"company_code": "1000", "supplier": newcomer,
                                                   "period_start": "2026-05-01", "period_end": "2026-05-31",
                                                   "quality_score": 4, "evaluated_on": "2026-06-01"}, format="json")
    assert evaluated.status_code == 201 and evaluated.data["grade"] == "B"  # quality 75 % only
    scorecard = buyer.get(f"{API}/scorecard/?company_code=1000").data["suppliers"]
    assert {row["supplier"]: row["grade"] for row in scorecard}[newcomer] == "B"


def test_api_rules_come_back_as_400_and_roles_are_enforced(srm, client_for):
    buyer = client_for(Role.PURCHASING_OFFICER)
    bare = buyer.post(f"{API}/applications/", {"company_code": "1000", "company_name": "No Papers"},
                      format="json").data
    refused = buyer.post(f"{API}/applications/{bare['id']}/approve/")
    assert refused.status_code == 400 and "registration number" in refused.data["non_field_errors"][0]
    one_supplier = buyer.post(f"{API}/rfqs/", {"purchasing_organization": "1000", "title": "x",
                                               "deadline": "2026-05-11", "suppliers": [srm.supplier.number],
                                               "items": [{"material": "TG-1001", "quantity": "5"}]}, format="json")
    assert one_supplier.status_code == 400
    assert buyer.get(f"{API}/scorecard/").status_code == 400  # company_code is required

    viewer = client_for(Role.VIEWER)
    assert viewer.get(f"{API}/rfqs/").status_code == 200
    assert viewer.post(f"{API}/applications/", {"company_code": "1000", "company_name": "x"},
                       format="json").status_code == 403
    assert client_for(Role.SALES_REP).get(f"{API}/contracts/").status_code == 403


def test_applicant_contact_details_are_need_to_know(srm, client_for, caplog):
    submitted = application(srm, company_name="Another Co", vat_number="4888888888")
    buyer_view = client_for(Role.PURCHASING_OFFICER).get(f"{API}/applications/{submitted.pk}/").data
    assert (buyer_view["contact_name"], buyer_view["phone"]) == ("Demo Sales", "+27 11 555 0200")
    viewer_view = client_for(Role.VIEWER).get(f"{API}/applications/{submitted.pk}/").data
    assert (viewer_view["contact_name"], viewer_view["email"], viewer_view["phone"]) == (None, None, None)
    assert viewer_view["company_name"] == "Another Co"
    for secret in ("Demo Sales", "gautengcomponents", "555 0200"):
        assert secret not in caplog.text  # never in the log


# --- graph -----------------------------------------------------------------------------------------------

def test_srm_reaches_the_graph_without_personal_data(srm, settings):
    settings.GRAPH_ENABLED = True
    rfq = quoted_rfq(srm)
    order = services.award_rfq(rfq, quote_of(rfq, srm.second), on=DAY + timedelta(days=8)).purchase_order
    contract = contract_with(srm)
    release = services.release_order(contract, order_date=DAY, items=[(srm.widget, D("10"))])
    services.evaluate_supplier(company_code=srm.company, supplier=srm.second, period_start=date(2026, 5, 1),
                               period_end=date(2026, 5, 31), quality_score=5, evaluated_on=date(2026, 6, 1))
    store = MemoryStore()
    assert processor.process_outbox(limit=100000, store=store)["failed"] == 0

    assert store.props("RFQ", "1000/RQ00000001")["status"] == "AWARDED"
    invited = {key: props["status"] for _, key, props in store.related("RFQ", "1000/RQ00000001", "INVITED")}
    assert invited == {srm.supplier.number: "LOST", srm.second.number: "AWARDED"}
    assert store.related("RFQ", "1000/RQ00000001", "AWARDED_AS")[0][1] == f"1000/{order.order_number}"
    assert store.related("Contract", "1000/CT00000001", "WITH")[0][1] == srm.supplier.number
    assert store.related("Contract", "1000/CT00000001", "RELEASED_AS")[0][1] == f"1000/{release.order_number}"
    assert store.related("Contract", "1000/CT00000001", "COVERS")[0][2]["net_price"] == D("180.00")
    grade = store.props("BusinessPartner", srm.second.number)
    assert (grade["supplier_grade"], grade["supplier_score"]) == ("A", D("100.0"))
    everything = repr(store.nodes) + repr(store.edges)
    for secret in ("Demo Sales", "gautengcomponents", "555 0200"):
        assert secret not in everything


def test_demo_command_runs_once(demo):
    out = StringIO()
    call_command("setup_demo_srm", stdout=out)
    assert "RQ00000001 awarded as purchase order PO00000001" in out.getvalue()
    assert (SupplierApplication.objects.count(), RequestForQuotation.objects.count(), Contract.objects.count(),
            SupplierEvaluation.objects.count()) == (2, 1, 1, 2)
    assert SupplierQuote.objects.filter(status="AWARDED").count() == 1
    call_command("setup_demo_srm", stdout=out)  # again: nothing new
    assert "nothing to do" in out.getvalue() and SupplierApplication.objects.count() == 2
