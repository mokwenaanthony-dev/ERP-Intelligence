"""``manage.py setup_demo_crm``: a small sales pipeline for demo company 1000 (Wave 7a).

Run after ``setup_demo_company``. Creates, through the CRM services (so every rule runs):

* campaigns SPRING-EXPO (trade show, budget R25,000) and WEB-2026 (website, R5,000);
* lead LD00000001 from the expo, converted into a new customer and opportunity OP00000001, quoted in
  QT00000001 (40 x TG-1001 and 10 x TG-1002 at list price: R12,950.00), sent and accepted: sales order
  in the customer's name, opportunity WON;
* lead LD00000002 from the website, qualified, converted, then lost to a cheaper competitor;
* lead LD00000003 still NEW, and LD00000004 disqualified;
* an open opportunity with Acme Retail in NEGOTIATION, its quotation sent and waiting;
* two service tickets for Acme Retail: one resolved within its SLA, one urgent and overdue.

The contacts are invented (``example.co.za``). Running it again changes nothing.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.crm import services
from apps.crm.models import Campaign, Lead, ServiceTicket
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import CompanyCode, SalesOrganization

D = Decimal


class Command(BaseCommand):
    help = "Create a demo CRM pipeline (campaigns, leads, opportunities, quotations, tickets) for company 1000."

    @transaction.atomic
    def handle(self, *args, **options):
        company = CompanyCode.objects.filter(code="1000").first()
        if company is None:
            raise CommandError("Run setup_demo_company first.")
        if Campaign.objects.filter(company_code=company, code="SPRING-EXPO").exists():
            self.stdout.write("Demo CRM data already exists: nothing to do.")
            return
        sales_org = SalesOrganization.objects.get(code="1000")
        widget, gadget = Material.objects.get(number="TG-1001"), Material.objects.get(number="TG-1002")
        acme = BusinessPartner.objects.get(name="Acme Retail (Pty) Ltd")

        expo = Campaign.objects.create(company_code=company, code="SPRING-EXPO", name="Spring trade expo",
                                       channel=Campaign.Channel.EVENT, start_date=date(2026, 5, 1),
                                       end_date=date(2026, 5, 3), budget=D("25000.00"))
        web = Campaign.objects.create(company_code=company, code="WEB-2026", name="Website enquiries 2026",
                                      channel=Campaign.Channel.WEB, start_date=date(2026, 3, 1),
                                      end_date=date(2027, 2, 28), budget=D("5000.00"))

        # 1. Expo lead -> customer -> quotation -> sales order: the whole path, won.
        lead = services.create_lead(company_code=company, company_name="Karoo Hardware (Pty) Ltd",
                                    contact_name="Demo Buyer", email="buyer@karoo.example.co.za",
                                    source=Lead.Source.TRADE_SHOW, campaign=expo)
        services.qualify_lead(lead)
        won = services.convert_lead(lead, title="Widgets for the new Karoo store",
                                    expected_close_date=date(2026, 6, 30), expected_value=D("12000"))
        quote = services.create_quotation(
            sales_organization=sales_org, customer=won.customer, opportunity=won, quotation_date=date(2026, 5, 10),
            valid_until=date(2026, 6, 10),
            items=[services.QuotationItemInput(widget, D("40")), services.QuotationItemInput(gadget, D("10"))])
        services.send_quotation(quote)
        quote = services.accept_quotation(quote, on=date(2026, 5, 20))

        # 2. Website lead, converted, then lost on price.
        lead = services.create_lead(company_code=company, company_name="Lowveld Builders CC",
                                    email="info@lowveld.example.co.za", source=Lead.Source.WEBSITE, campaign=web)
        services.qualify_lead(lead)
        lost = services.convert_lead(lead, title="Gadgets for site offices", expected_close_date=date(2026, 7, 15),
                                     expected_value=D("4990"))
        services.lose_opportunity(lost, reason="Chose a cheaper competitor", on=date(2026, 6, 5))

        # 3. A new lead and a disqualified one.
        services.create_lead(company_code=company, company_name="Garden Route Traders", source=Lead.Source.WEBSITE,
                             campaign=web)
        student = services.create_lead(company_code=company, company_name="Varsity Project Group",
                                       source=Lead.Source.REFERRAL)
        services.disqualify_lead(student, reason="Student project, no purchase planned")

        # 4. An existing customer in negotiation.
        open_deal = services.create_opportunity(company_code=company, customer=acme, title="Winter restock",
                                                expected_close_date=date(2026, 8, 31), stage="NEGOTIATION",
                                                expected_value=D("19900"))
        waiting = services.create_quotation(
            sales_organization=sales_org, customer=acme, opportunity=open_deal, quotation_date=date(2026, 6, 1),
            valid_until=date(2026, 7, 31), items=[services.QuotationItemInput(widget, D("100"))])
        services.send_quotation(waiting)
        services.log_activity(kind="CALL", subject="Follow up the winter restock quotation", due_date=date(2026, 6, 8),
                              opportunity=open_deal)

        # 5. After-sales service: one resolved in time, one urgent and overdue.
        start = datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)
        done = services.open_ticket(company_code=company, customer=acme, subject="Two widgets arrived scratched",
                                    priority=ServiceTicket.Priority.MEDIUM, material=widget, opened_at=start)
        services.start_ticket(done)
        services.resolve_ticket(done, resolution="Replacement widgets sent", at=start + timedelta(hours=6))
        services.open_ticket(company_code=company, customer=acme, subject="Gadget stopped working",
                             priority=ServiceTicket.Priority.URGENT, material=gadget,
                             opened_at=start + timedelta(days=1))

        self.stdout.write(self.style.SUCCESS(
            f"Demo CRM: 2 campaigns, 4 leads, 3 opportunities (won {won.opportunity_number}, lost "
            f"{lost.opportunity_number}, open {open_deal.opportunity_number}), quotation {quote.quotation_number} "
            f"accepted as sales order {quote.sales_order.order_number}, 2 service tickets"))
