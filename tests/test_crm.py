"""CRM (Wave 7a): from a lead to a sales order, after-sales tickets, the pipeline and campaign reports.

Every status change is repeated to prove it is idempotent (ADR 0010), and lead contact details are
checked to stay out of the log, other roles' API answers and the knowledge graph (ADR 0012).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role
from apps.crm import services
from apps.crm.models import Campaign, Lead, Opportunity, Quotation, ServiceTicket
from apps.crm.services import CRMError, QuotationItemInput
from apps.graph import processor
from apps.graph.store import MemoryStore
from apps.masterdata.models import CustomerRole, Material
from apps.organization.models import SalesOrganization
from apps.sales.models import SalesOrder, SalesPrice

pytestmark = pytest.mark.django_db
D = Decimal
API = "/api/crm"


@pytest.fixture
def crm(demo):
    demo.sales_org = SalesOrganization.objects.get(code="1000")
    demo.widget = Material.objects.get(number="TG-1001")  # R199.00 on the price list
    demo.gadget = Material.objects.get(number="TG-1002")  # R499.00
    demo.expo = Campaign.objects.create(company_code=demo.company, code="SPRING-EXPO", name="Spring expo",
                                        channel="EVENT", start_date=date(2026, 5, 1), end_date=date(2026, 5, 3),
                                        budget=D("25000.00"))
    return demo


def qualified_lead(crm, **extra):
    lead = services.create_lead(company_code=crm.company, company_name="Karoo Hardware (Pty) Ltd",
                                contact_name="Demo Buyer", email="buyer@karoo.example.co.za", phone="+27 11 555 0100",
                                source="TRADE_SHOW", campaign=crm.expo, **extra)
    return services.qualify_lead(lead)


def sent_quotation(crm, opportunity, **items):
    quotation = services.create_quotation(
        sales_organization=crm.sales_org, customer=opportunity.customer, opportunity=opportunity,
        quotation_date=date(2026, 5, 10), valid_until=date(2026, 6, 10),
        items=[QuotationItemInput(crm.widget, D("40")), QuotationItemInput(crm.gadget, D("10"))])
    return services.send_quotation(quotation)


# --- leads -----------------------------------------------------------------------------------------

def test_a_lead_becomes_a_customer_and_an_opportunity(crm):
    lead = qualified_lead(crm)
    assert lead.lead_number == "LD00000001" and lead.status == "QUALIFIED"
    opportunity = services.convert_lead(lead, title="Store opening", expected_close_date=date(2026, 6, 30),
                                        expected_value=D("12000"))
    lead.refresh_from_db()
    customer = opportunity.customer
    assert (lead.status, lead.business_partner, lead.opportunity) == ("CONVERTED", customer, opportunity)
    assert customer.name == "Karoo Hardware (Pty) Ltd" and customer.email == "buyer@karoo.example.co.za"
    role = CustomerRole.objects.get(business_partner=customer, company_code=crm.company)
    assert role.reconciliation_account.number == "1100"  # receivables: the customer can be invoiced at once
    assert (opportunity.opportunity_number, opportunity.stage, opportunity.probability) == (
        "OP00000001", "QUALIFICATION", 25)
    assert opportunity.campaign == crm.expo  # the campaign gets the credit


def test_lead_transitions_are_idempotent(crm):
    lead = qualified_lead(crm)
    assert services.qualify_lead(lead).status == "QUALIFIED"  # again: unchanged
    first = services.convert_lead(lead, title="x", expected_close_date=date(2026, 6, 30))
    again = services.convert_lead(lead, title="y", expected_close_date=date(2026, 7, 31))
    assert again == first and Opportunity.objects.count() == 1  # the same opportunity, no second customer
    with pytest.raises(CRMError, match="cannot be disqualified"):
        services.disqualify_lead(lead, "too late")


def test_a_lead_can_join_an_existing_customer(crm):
    opportunity = services.convert_lead(qualified_lead(crm), title="Acme branch", customer=crm.customer,
                                        expected_close_date=date(2026, 6, 30))
    assert opportunity.customer == crm.customer
    assert CustomerRole.objects.filter(business_partner=crm.customer).count() == 1  # not duplicated


def test_disqualified_and_new_leads_cannot_convert(crm):
    lead = services.create_lead(company_code=crm.company, company_name="Varsity Project Group")
    with pytest.raises(CRMError, match="Only a QUALIFIED lead"):
        services.convert_lead(lead, title="x", expected_close_date=date(2026, 6, 30))
    services.disqualify_lead(lead, "Student project")
    assert services.disqualify_lead(lead, "Student project").status == "DISQUALIFIED"  # repeat: unchanged
    with pytest.raises(CRMError, match="Only a NEW lead"):
        services.qualify_lead(lead)
    with pytest.raises(CRMError, match="name of the company"):
        services.create_lead(company_code=crm.company, company_name="  ")


# --- opportunities and quotations --------------------------------------------------------------------

def test_quotation_to_sales_order_wins_the_opportunity(crm):
    opportunity = services.convert_lead(qualified_lead(crm), title="Store opening",
                                        expected_close_date=date(2026, 6, 30), expected_value=D("12000"))
    quotation = sent_quotation(crm, opportunity)
    assert quotation.net_total == D("12950.00")  # 40 x 199 + 10 x 499, from the price list
    opportunity.refresh_from_db()
    assert (opportunity.stage, opportunity.probability, opportunity.expected_value) == ("PROPOSAL", 50, D("12950.00"))

    # The price list changes after the quote: the customer still gets the quoted price.
    SalesPrice.objects.create(material=crm.widget, sales_organization=crm.sales_org, price=D("249.00"),
                              valid_from=date(2026, 5, 15))
    accepted = services.accept_quotation(quotation, on=date(2026, 5, 20))
    order = accepted.sales_order
    assert (order.customer, order.order_date, order.customer_reference) == (
        opportunity.customer, date(2026, 5, 20), quotation.quotation_number)
    assert [(i.material.number, i.quantity, i.net_price) for i in order.items.order_by("item_number")] == [
        ("TG-1001", D("40.000"), D("199.00")), ("TG-1002", D("10.000"), D("499.00"))]
    assert order.net_total == D("12950.00")
    opportunity.refresh_from_db()
    assert (opportunity.stage, opportunity.probability, opportunity.closed_on) == ("WON", 100, date(2026, 5, 20))

    again = services.accept_quotation(quotation, on=date(2026, 5, 21))  # a retry: the same order, no second one
    assert again.sales_order == order and SalesOrder.objects.filter(customer=opportunity.customer).count() == 1


def test_quotation_rules(crm):
    opportunity = services.create_opportunity(company_code=crm.company, customer=crm.customer, title="Restock",
                                              expected_close_date=date(2026, 8, 31))
    quotation = sent_quotation(crm, opportunity)
    with pytest.raises(CRMError, match="expired on 2026-06-10"):
        services.accept_quotation(quotation, on=date(2026, 6, 11))
    services.reject_quotation(quotation)
    assert services.reject_quotation(quotation).status == "REJECTED"  # repeat: unchanged
    with pytest.raises(CRMError, match="Only a SENT quotation"):
        services.accept_quotation(quotation, on=date(2026, 6, 1))
    with pytest.raises(CRMError, match="at least one item"):
        services.create_quotation(sales_organization=crm.sales_org, customer=crm.customer, items=[],
                                  quotation_date=date(2026, 5, 1), valid_until=date(2026, 5, 31))
    with pytest.raises(CRMError, match="expire before its date"):
        services.create_quotation(sales_organization=crm.sales_org, customer=crm.customer,
                                  items=[QuotationItemInput(crm.widget, D("1"))],
                                  quotation_date=date(2026, 5, 31), valid_until=date(2026, 5, 1))
    with pytest.raises(CRMError, match="No price"):
        services.create_quotation(sales_organization=crm.sales_org, customer=crm.customer,
                                  items=[QuotationItemInput(crm.widget, D("1"))],
                                  quotation_date=date(2020, 1, 1), valid_until=date(2020, 1, 31))
    with pytest.raises(CRMError, match="not a customer"):
        services.create_opportunity(company_code=crm.company, customer=crm.supplier, title="x",
                                    expected_close_date=date(2026, 8, 31))


def test_losing_an_opportunity_rejects_its_open_quotations(crm):
    opportunity = services.create_opportunity(company_code=crm.company, customer=crm.customer, title="Restock",
                                              expected_close_date=date(2026, 8, 31), stage="NEGOTIATION")
    quotation = sent_quotation(crm, opportunity)
    services.lose_opportunity(opportunity, reason="Cheaper competitor", on=date(2026, 6, 5))
    assert services.lose_opportunity(opportunity, reason="Cheaper competitor", on=date(2026, 6, 6)).closed_on == \
        date(2026, 6, 5)  # repeat: unchanged
    quotation.refresh_from_db()
    assert quotation.status == "REJECTED"
    with pytest.raises(CRMError, match="closed"):
        services.move_opportunity(opportunity, "PROPOSAL")


def test_stages_move_both_ways_with_their_probability(crm):
    opportunity = services.create_opportunity(company_code=crm.company, customer=crm.customer, title="Restock",
                                              expected_close_date=date(2026, 8, 31))
    assert services.move_opportunity(opportunity, "NEGOTIATION").probability == 75
    assert services.move_opportunity(opportunity, "PROPOSAL", probability=60).probability == 60
    with pytest.raises(CRMError, match="won by accepting"):
        services.move_opportunity(opportunity, "WON")


# --- service tickets ---------------------------------------------------------------------------------

def test_tickets_follow_their_sla(crm):
    opened = datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)
    ticket = services.open_ticket(company_code=crm.company, customer=crm.customer, subject="Scratched widgets",
                                  priority="HIGH", material=crm.widget, opened_at=opened)
    assert (ticket.ticket_number, ticket.due_at) == ("ST00000001", opened + timedelta(hours=8))
    services.start_ticket(ticket)
    assert services.start_ticket(ticket).status == "IN_PROGRESS"  # repeat: unchanged
    urgent = services.open_ticket(company_code=crm.company, customer=crm.customer, subject="Broken gadget",
                                  priority="URGENT", opened_at=opened)
    assert [t.ticket_number for t in services.overdue_tickets(crm.company, opened + timedelta(hours=9))] == [
        urgent.ticket_number, ticket.ticket_number]  # most overdue first

    services.resolve_ticket(ticket, resolution="Replacements sent", at=opened + timedelta(hours=5))
    assert services.resolve_ticket(ticket, resolution="again").resolution == "Replacements sent"
    services.reopen_ticket(ticket)
    services.resolve_ticket(ticket, resolution="Second replacement", at=opened + timedelta(hours=7))
    services.close_ticket(ticket)
    assert services.close_ticket(ticket).status == "CLOSED"
    with pytest.raises(CRMError, match="must be RESOLVED"):
        services.close_ticket(urgent)


# --- reports -----------------------------------------------------------------------------------------

def test_pipeline_and_campaign_performance(crm):
    won = services.convert_lead(qualified_lead(crm), title="Store", expected_close_date=date(2026, 6, 30))
    services.accept_quotation(sent_quotation(crm, won), on=date(2026, 5, 20))
    services.create_opportunity(company_code=crm.company, customer=crm.customer, title="Restock",
                                expected_close_date=date(2026, 8, 31), stage="NEGOTIATION", expected_value=D("20000"))
    lost = services.create_opportunity(company_code=crm.company, customer=crm.customer, title="Old deal",
                                       expected_close_date=date(2026, 7, 31))
    services.lose_opportunity(lost, reason="No budget", on=date(2026, 6, 1))

    report = services.pipeline(crm.company)
    assert report["open"] == {"count": 1, "value": D("20000.00"), "weighted_value": D("15000.00")}
    assert report["forecast_by_month"] == [{"month": "2026-08", "weighted_value": D("15000.00")}]
    assert report["won"] == {"count": 1, "value": D("12950.00")}
    assert report["win_rate"] == D("50.0")

    [expo] = services.campaign_performance(crm.company)
    assert (expo["leads"], expo["converted_leads"], expo["won"], expo["won_value"]) == (1, 1, 1, D("12950.00"))
    assert expo["return_on_budget"] == D("0.52")  # R12,950 won on R25,000 spent


# --- API -----------------------------------------------------------------------------------------------

def test_the_sales_team_runs_the_whole_path_through_the_api(crm, client_for):
    rep = client_for(Role.SALES_REP)
    lead = rep.post(f"{API}/leads/", {"company_code": "1000", "company_name": "Karoo Hardware (Pty) Ltd",
                                      "contact_name": "Demo Buyer", "email": "buyer@karoo.example.co.za",
                                      "source": "TRADE_SHOW", "campaign": "SPRING-EXPO"}, format="json")
    assert lead.status_code == 201, lead.data
    lead_id = lead.data["id"]
    assert rep.post(f"{API}/leads/{lead_id}/qualify/").data["status"] == "QUALIFIED"
    converted = rep.post(f"{API}/leads/{lead_id}/convert/", {"expected_close_date": "2026-06-30",
                                                             "expected_value": "12000"}, format="json")
    assert converted.status_code == 200 and converted.data["stage"] == "QUALIFICATION"
    items = [{"material": "TG-1001", "quantity": "40"},
             {"material": "TG-1002", "quantity": "10", "net_price": "450.00"}]
    quote = rep.post(f"{API}/quotations/", {
        "sales_organization": "1000", "customer": converted.data["customer"],
        "opportunity": converted.data["opportunity_number"], "quotation_date": "2026-05-10",
        "valid_until": "2026-06-10", "items": items}, format="json")
    assert quote.status_code == 201, quote.data
    assert quote.data["net_total"] == "12460.00"  # 40 x 199 + 10 x 450 (a negotiated price)
    assert rep.post(f"{API}/quotations/{quote.data['id']}/send/").data["status"] == "SENT"
    accepted = rep.post(f"{API}/quotations/{quote.data['id']}/accept/", {"on": "2026-05-20"}, format="json")
    assert accepted.status_code == 200 and accepted.data["sales_order"] == "SO00000001"
    # .json() is the rendered answer: money as exact decimal strings (ADR 0008).
    assert rep.get(f"{API}/pipeline/?company_code=1000").json()["won"]["value"] == "12460.00"
    campaigns = rep.get(f"{API}/campaign-performance/?company_code=1000").json()["campaigns"]
    assert campaigns[0]["won_value"] == "12460.00"


def test_api_rules_come_back_as_400_and_roles_are_enforced(crm, client_for):
    rep = client_for(Role.SALES_REP)
    lead = rep.post(f"{API}/leads/", {"company_code": "1000", "company_name": "New Co"}, format="json").data
    refused = rep.post(f"{API}/leads/{lead['id']}/convert/", {"expected_close_date": "2026-06-30"}, format="json")
    assert refused.status_code == 400 and "Only a QUALIFIED lead" in refused.data["non_field_errors"][0]
    assert rep.post(f"{API}/leads/", {"company_code": "1000", "company_name": "x", "campaign": "NOPE"},
                    format="json").status_code == 400
    assert rep.get(f"{API}/pipeline/").status_code == 400  # company_code is required

    viewer = client_for(Role.VIEWER)
    assert viewer.get(f"{API}/leads/").status_code == 200
    assert viewer.post(f"{API}/leads/", {"company_code": "1000", "company_name": "x"}, format="json") \
        .status_code == 403
    assert client_for(Role.HR_ADMIN).get(f"{API}/leads/").status_code == 403


def test_lead_contact_details_are_need_to_know(crm, client_for, caplog):
    lead = qualified_lead(crm)
    rep_view = client_for(Role.SALES_REP).get(f"{API}/leads/{lead.pk}/").data
    assert (rep_view["contact_name"], rep_view["email"]) == ("Demo Buyer", "buyer@karoo.example.co.za")
    viewer_view = client_for(Role.VIEWER).get(f"{API}/leads/{lead.pk}/").data
    assert (viewer_view["contact_name"], viewer_view["email"], viewer_view["phone"]) == (None, None, None)
    assert viewer_view["company_name"] == "Karoo Hardware (Pty) Ltd"
    for secret in ("Demo Buyer", "buyer@karoo", "555 0100"):
        assert secret not in caplog.text  # never in the log


def test_tickets_and_activities_through_the_api(crm, client_for):
    rep = client_for(Role.SALES_REP)
    ticket = rep.post(f"{API}/tickets/", {"company_code": "1000", "customer": crm.customer.number,
                                          "subject": "Broken gadget", "priority": "URGENT", "material": "TG-1002"},
                      format="json")
    assert ticket.status_code == 201 and ticket.data["ticket_number"] == "ST00000001"
    activity = rep.post(f"{API}/activities/", {"company_code": "1000", "kind": "CALL", "subject": "Call back",
                                               "due_date": "2026-06-03", "ticket": "ST00000001"}, format="json")
    assert activity.status_code == 201 and activity.data["ticket"] == "ST00000001"
    done = rep.post(f"{API}/activities/{activity.data['id']}/complete/", {"on": "2026-06-03"}, format="json")
    assert done.data["completed_on"] == "2026-06-03"
    assert rep.get(f"{API}/activities/?open=true").data["count"] == 0
    resolved = rep.post(f"{API}/tickets/{ticket.data['id']}/resolve/", {"resolution": "Replaced"}, format="json")
    assert resolved.data["status"] == "RESOLVED"
    two_targets = rep.post(f"{API}/activities/", {"company_code": "1000", "kind": "TASK", "subject": "x",
                                                  "due_date": "2026-06-03", "ticket": "ST00000001",
                                                  "lead": "LD99999999"}, format="json")
    assert two_targets.status_code == 400
    overdue = rep.get(f"{API}/overdue-tickets/?company_code=1000").data
    assert overdue["tickets"] == []  # resolved in time


# --- graph -----------------------------------------------------------------------------------------------

def test_crm_reaches_the_graph_without_personal_data(crm, settings):
    settings.GRAPH_ENABLED = True
    opportunity = services.convert_lead(qualified_lead(crm), title="Store", expected_close_date=date(2026, 6, 30))
    quotation = services.accept_quotation(sent_quotation(crm, opportunity), on=date(2026, 5, 20))
    ticket = services.open_ticket(company_code=crm.company, customer=opportunity.customer, subject="Late delivery",
                                  sales_order=quotation.sales_order)
    store = MemoryStore()
    assert processor.process_outbox(limit=100000, store=store)["failed"] == 0

    customer = opportunity.customer.number
    assert store.props("Opportunity", "1000/OP00000001")["stage"] == "WON"
    assert store.related("BusinessPartner", customer, "HAS_OPPORTUNITY")[0][1] == "1000/OP00000001"
    assert store.related("Opportunity", "1000/OP00000001", "FROM_CAMPAIGN")[0][1] == "1000/SPRING-EXPO"
    assert store.related("Opportunity", "1000/OP00000001", "QUOTED_IN")[0][1] == "1000/QT00000001"
    assert store.related("Quotation", "1000/QT00000001", "ORDERED_AS")[0][1] == "1000/SO00000001"
    assert store.props("Quotation", "1000/QT00000001")["status"] == "ACCEPTED"
    assert store.related("ServiceTicket", f"1000/{ticket.ticket_number}", "ABOUT")[0][1] == "1000/SO00000001"
    everything = repr(store.nodes) + repr(store.edges)
    assert "Lead" not in {label for label, _ in store.nodes}
    for secret in ("Demo Buyer", "karoo.example", "555 0100"):
        assert secret not in everything


def test_demo_command_runs_once(crm):
    Campaign.objects.all().delete()
    out = StringIO()
    call_command("setup_demo_crm", stdout=out)
    assert "accepted as sales order SO00000001" in out.getvalue()
    assert (Lead.objects.count(), Opportunity.objects.count(), Quotation.objects.count(),
            ServiceTicket.objects.count()) == (4, 3, 2, 2)
    assert Opportunity.objects.get(opportunity_number="OP00000001").stage == "WON"
    call_command("setup_demo_crm", stdout=out)  # again: nothing new
    assert "nothing to do" in out.getvalue() and Lead.objects.count() == 4
