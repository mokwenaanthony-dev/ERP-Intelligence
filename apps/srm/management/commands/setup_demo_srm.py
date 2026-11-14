"""``manage.py setup_demo_srm``: supplier relationship data for demo company 1000 (Wave 7b).

Run after ``setup_demo_company``. Creates, through the SRM and purchasing services (so every rule runs):

* application OB00000001 from Gauteng Components (Pty) Ltd: registration and VAT numbers given,
  B-BBEE level 2, approved, so it becomes the second supplier of the company;
* application OB00000002 from a company without a registration or VAT number: rejected;
* request for quotation RQ00000001 for 50 x TG-1001 and 20 x TG-1002, sent to Highveld Supplies CC and
  Gauteng Components; both quote, Gauteng is cheaper and is awarded: a purchase order at its prices,
  received on time and in full and invoiced at the order price;
* contract CT00000001 with Highveld for the fiscal year (TG-1001 at R180.00 up to 500 units) and one
  release of 100 units, received five days late;
* an evaluation of both suppliers for the fiscal year so far, so the scorecard shows two grades.

Dates are taken from the fiscal year that contains today, or else the latest one (postings need an
open period), so they differ from year to year. The contact details are invented
(``example.co.za``). Running it again changes nothing.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.finance.models import FiscalYear
from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import CompanyCode, PurchasingOrganization
from apps.purchasing import services as purchasing
from apps.srm import services
from apps.srm.models import SupplierApplication

D = Decimal


class Command(BaseCommand):
    help = "Create demo SRM data (applications, an awarded RFQ, a contract, evaluations) for company 1000."

    @transaction.atomic
    def handle(self, *args, **options):
        company = CompanyCode.objects.filter(code="1000").first()
        if company is None:
            raise CommandError("Run setup_demo_company first.")
        if SupplierApplication.objects.filter(company_code=company).exists():
            self.stdout.write("Demo SRM data already exists: nothing to do.")
            return
        today = date.today()
        # The fiscal year containing today, else the latest one (a demo built last year still works).
        fiscal_year = (FiscalYear.objects.filter(company_code=company, start_date__lte=today, end_date__gte=today)
                       .first() or FiscalYear.objects.filter(company_code=company).order_by("-start_date").first())
        if fiscal_year is None:
            raise CommandError("Company 1000 has no fiscal year: run setup_demo_company.")
        anchor = min(today, fiscal_year.end_date)
        # Sixty days back (but not before the fiscal year starts), so every date below is in the year.
        start = max(fiscal_year.start_date, anchor - timedelta(days=60))
        org = PurchasingOrganization.objects.get(code="1000")
        widget, gadget = Material.objects.get(number="TG-1001"), Material.objects.get(number="TG-1002")
        highveld = BusinessPartner.objects.get(name="Highveld Supplies CC")

        # 1. Onboarding: one application passes due diligence, one does not.
        application = services.submit_application(
            company_code=company, company_name="Gauteng Components (Pty) Ltd", registration_number="2019/123456/07",
            vat_number="4555555555", bbbee_level=2, supplies="Widgets and gadgets", contact_name="Demo Sales",
            email="sales@gautengcomponents.example.co.za")
        gauteng = services.approve_application(application).business_partner
        rejected = services.submit_application(company_code=company, company_name="Quick Deals Trading",
                                               supplies="Anything, cheap")
        services.reject_application(rejected, reason="No company registration or VAT number")

        # 2. A request for quotation: two quotes compared, the cheaper one awarded.
        rfq = services.create_rfq(
            purchasing_organization=org, title="Widgets and gadgets for the winter season",
            deadline=start + timedelta(days=7), suppliers=[highveld, gauteng],
            items=[services.RFQItemInput(widget, D("50")), services.RFQItemInput(gadget, D("20"))])
        services.send_rfq(rfq)
        quotes = {quote.supplier_id: quote for quote in rfq.quotes.all()}
        services.submit_quote(quotes[highveld.pk], prices={widget: D("185.00"), gadget: D("460.00")},
                              delivery_days=5, submitted_on=start + timedelta(days=2), supplier_reference="HV-Q-881")
        services.submit_quote(quotes[gauteng.pk], prices={widget: D("178.00"), gadget: D("455.00")},
                              delivery_days=7, submitted_on=start + timedelta(days=3), supplier_reference="GC-2026-14")
        rfq = services.award_rfq(rfq, quotes[gauteng.pk], on=start + timedelta(days=8))
        awarded = purchasing.release_order(rfq.purchase_order)
        purchasing.post_goods_receipt(awarded, receipt_date=awarded.delivery_date, delivery_note="GC-DN-301")
        purchasing.post_supplier_invoice(awarded, invoice_date=awarded.delivery_date + timedelta(days=1),
                                         supplier_reference="GC-INV-301")

        # 3. A contract with Highveld and one release against it, delivered late.
        contract = services.create_contract(
            purchasing_organization=org, supplier=highveld, valid_from=fiscal_year.start_date,
            valid_to=fiscal_year.end_date, items=[services.ContractItemInput(widget, D("180.00"), D("500"))])
        released = services.release_order(contract, order_date=start + timedelta(days=10), items=[(widget, D("100"))],
                                          delivery_date=start + timedelta(days=20))
        released = purchasing.release_order(released)
        purchasing.post_goods_receipt(released, receipt_date=start + timedelta(days=25), delivery_note="HV-DN-77")

        # 4. Evaluate both suppliers for the fiscal year so far.
        period_end = min(max(anchor, start + timedelta(days=25)), fiscal_year.end_date)
        grades = {}
        for supplier, quality in ((gauteng, 5), (highveld, 3)):
            evaluation = services.evaluate_supplier(company_code=company, supplier=supplier,
                                                    period_start=fiscal_year.start_date, period_end=period_end,
                                                    quality_score=quality, evaluated_on=period_end)
            grades[supplier.name] = f"{evaluation.overall_score} ({evaluation.grade})"

        self.stdout.write(self.style.SUCCESS(
            f"Demo SRM: supplier {gauteng.number} onboarded ({application.application_number}), "
            f"{rejected.application_number} rejected, {rfq.rfq_number} awarded as purchase order "
            f"{awarded.order_number}, contract {contract.contract_number} released as {released.order_number}; "
            f"scores {', '.join(f'{name} {score}' for name, score in grades.items())}"))
