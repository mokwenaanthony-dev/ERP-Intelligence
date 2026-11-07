"""Generate months of realistic trading for the demo company, so reports and dashboards have history.

Usage:
    python manage.py seed_data                     # users and the demo company first
    python manage.py seed_demo_activity            # every completed month of the fiscal year so far
    python manage.py seed_demo_activity --months 3

Each month it buys, receives and pays for stock, sells to three customers (orders, deliveries,
invoices, customer payments), pays rent and utilities, runs payroll, pays net salaries, pays
last month's PAYE, UIF and SDL to SARS and settles last month's VAT. Everything goes through the
normal business services, so every rule applies and the books stay balanced. The data is random
but repeatable (``--seed``).
"""
import calendar
import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from apps.analytics import services as analytics
from apps.controlling.models import CostCenter
from apps.finance import services as finance
from apps.finance.models import FiscalYear, GLAccount, JournalLine, TaxCode
from apps.hr import services as hr
from apps.inventory import services as inventory
from apps.masterdata.models import BusinessPartner, CustomerRole, Material, SupplierRole, UnitOfMeasure
from apps.organization.models import CompanyCode, Plant, PurchasingOrganization, SalesOrganization, StorageLocation
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder
from apps.sales import services as sales
from apps.sales.models import SalesOrder, SalesPrice

D = Decimal
CUSTOMER, SUPPLIER = GLAccount.ReconciliationType.CUSTOMER, GLAccount.ReconciliationType.SUPPLIER

# name, VAT number, payment terms (days), pays late
CUSTOMERS = [
    ("Acme Retail (Pty) Ltd", "4111111111", 30, False),
    ("Karoo Outfitters CC", "4333333333", 30, False),
    ("Coastal Hardware (Pty) Ltd", "4444444444", 30, True),  # pays about a month late
]
# name, VAT number
SUPPLIERS = [("Highveld Supplies CC", "4222222222"), ("Gauteng Components (Pty) Ltd", "4555555555")]
# material, description, sales price, purchase price, supplier index, monthly purchase range
MATERIALS = [
    ("TG-1001", "Standard widget", "199.00", "115.00", 0, (330, 380)),
    ("TG-1002", "Deluxe gadget", "499.00", "290.00", 0, (320, 370)),
    ("TG-1003", "Premium kit", "899.00", "480.00", 1, (300, 350)),
]


class Command(BaseCommand):
    help = "Generate months of purchasing, sales, payroll and payments for the demo company."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, help="How many months from the start of the fiscal year "
                                                       "(default: every completed month so far).")
        parser.add_argument("--seed", type=int, default=2027, help="Random seed (same seed, same data).")

    @transaction.atomic
    def handle(self, *args, months=None, seed=2027, **options):
        self.company = CompanyCode.objects.select_related("chart_of_accounts").filter(code="1000").first()
        if self.company is None:
            raise CommandError("Run `python manage.py seed_data` (or setup_demo_company) first.")
        if SalesOrder.objects.filter(company_code=self.company).exists() or \
                PurchaseOrder.objects.filter(company_code=self.company).exists():
            raise CommandError("The demo company already has sales or purchases; activity is generated only once.")
        fiscal_year = FiscalYear.objects.filter(company_code=self.company).order_by("-year").first()
        periods = self._months(fiscal_year, months)
        if not periods:
            raise CommandError("No completed month in the fiscal year yet; pass --months.")

        self.rng = random.Random(seed)  # nosec B311 - demo data, not security
        self._master_data()
        for year, month in periods:
            self._month(year, month)
            self.stdout.write(self.style.SUCCESS(f"  {year}-{month:02d} posted"))
        analytics.refresh_views()
        self.stdout.write(self.style.SUCCESS(
            f"Generated {len(periods)} month(s) of activity for company code {self.company.code}."
        ))

    # --- set-up --------------------------------------------------------------------------

    @staticmethod
    def _months(fiscal_year, months):
        year, month = fiscal_year.start_date.year, fiscal_year.start_date.month
        result = []
        while len(result) < 12:
            last_day = date(year, month, calendar.monthrange(year, month)[1])
            if months is None and last_day >= date.today():
                break
            result.append((year, month))
            if months is not None and len(result) == months:
                break
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return result

    def _account(self, number):
        return GLAccount.objects.get(chart_of_accounts=self.company.chart_of_accounts, number=number)

    def _master_data(self):
        receivables, payables = self._account("1100"), self._account("2000")
        self.customers = []
        for name, vat, terms, late in CUSTOMERS:
            partner = BusinessPartner.objects.filter(name=name).first() or BusinessPartner.objects.create(
                name=name, vat_number=vat, city="Johannesburg")
            CustomerRole.objects.get_or_create(business_partner=partner, company_code=self.company, defaults={
                "reconciliation_account": receivables, "payment_terms_days": terms})
            self.customers.append((partner, late))
        self.suppliers = []
        for name, vat in SUPPLIERS:
            partner = BusinessPartner.objects.filter(name=name).first() or BusinessPartner.objects.create(
                name=name, vat_number=vat, city="Johannesburg")
            SupplierRole.objects.get_or_create(business_partner=partner, company_code=self.company,
                                               defaults={"reconciliation_account": payables})
            self.suppliers.append(partner)
        sales_org = SalesOrganization.objects.get(company_code=self.company)
        fiscal_start = FiscalYear.objects.filter(company_code=self.company).order_by("year").first().start_date
        self.materials = []
        for number, description, price, cost, supplier, purchase_range in MATERIALS:
            material, _ = Material.objects.get_or_create(number=number, defaults={
                "description": description, "base_unit": UnitOfMeasure.objects.get(code="EA"),
                "material_type": Material.MaterialType.TRADING_GOODS})
            SalesPrice.objects.get_or_create(material=material, sales_organization=sales_org,
                                             valid_from=fiscal_start, defaults={"price": D(price)})
            self.materials.append((material, D(cost), self.suppliers[supplier], purchase_range))
        self.plant = Plant.objects.get(company_code=self.company, code="1000")
        self.store = StorageLocation.objects.get(plant=self.plant, code="0001")
        self.sales_org = sales_org
        self.purchasing_org = PurchasingOrganization.objects.get(company_code=self.company)
        self.bank = self._account("1000")
        self.input_vat = TaxCode.objects.get(chart_of_accounts=self.company.chart_of_accounts, code="I1")
        self.previous_payroll = None

    # --- one month -------------------------------------------------------------------------

    def _month(self, year, month):
        last = calendar.monthrange(year, month)[1]

        def day(n):
            return date(year, month, min(n, last))

        self._pay_sars(day(7))
        self._purchases(year, month, day)
        self._overheads(day)
        self._sales(day, last)
        self._settle_vat(day(25), date(year, month, 1))
        self._pay_suppliers(day(25), date(year, month, 1))
        self._collect_from_customers(day(27), date(year, month, 1))
        run = hr.post_payroll_run(self.company, year=year, month=month)
        self._post("Net salaries paid", day(last), [(self._account("2500"), "D", run.net), (self.bank, "C", run.net)])
        self.previous_payroll = run

    def _purchases(self, year, month, day):
        for supplier in self.suppliers:
            lines = [(material, cost, purchase_range) for material, cost, owner, purchase_range in self.materials
                     if owner == supplier]
            order = purchasing.create_purchase_order(
                purchasing_organization=self.purchasing_org, supplier=supplier, order_date=day(1),
                items=[purchasing.OrderItemInput(material, D(self.rng.randint(*purchase_range)), cost)
                       for material, cost, purchase_range in lines],
            )
            purchasing.release_order(order)
            purchasing.post_goods_receipt(order, receipt_date=day(3))
            items = None
            if self.rng.random() < 0.4:  # sometimes the supplier bills a little above the order price
                first = order.items.first()
                items = [purchasing.InvoiceItemInput(i, i.quantity, (i.net_price * D("1.02")).quantize(D("0.01"))
                                                     if i.pk == first.pk else None) for i in order.items.all()]
            purchasing.post_supplier_invoice(order, invoice_date=day(6), items=items,
                                             supplier_reference=f"{supplier.number}-{year}{month:02d}")

    def _overheads(self, day):
        management, warehouse = CostCenter.objects.get(code="1000"), CostCenter.objects.get(code="3000")
        rent = D("28000.00")
        utilities = D(self.rng.randint(600000, 900000)) / 100
        for text, account, amount, centre, when in (("Office and warehouse rent", "6100", rent, management, day(1)),
                                                    ("Electricity and water", "6200", utilities, warehouse, day(15))):
            gross = amount + finance.calculate_tax(amount, self.input_vat)
            finance.post_journal_entry(company_code=self.company, posting_date=when, header_text=text, lines=[
                finance.LineInput(self._account(account), "D", amount, cost_center=centre, tax_code=self.input_vat),
                finance.LineInput(self.bank, "C", gross),
            ])

    def _sales(self, day, last):
        order_days = sorted(self.rng.randint(4, max(4, last - 4)) for _ in range(self.rng.randint(15, 19)))
        for order_day in order_days:
            customer, _ = self.rng.choice(self.customers)
            items = []
            for material, *_ in self.rng.sample(self.materials, self.rng.randint(1, 3)):
                available, _value = inventory.plant_stock(material, self.plant)
                quantity = min(D(self.rng.randint(15, 45)), available)
                if quantity > 0:
                    items.append(sales.OrderItemInput(material, quantity))
            if not items:
                continue
            order = sales.create_sales_order(sales_organization=self.sales_org, customer=customer,
                                             order_date=day(order_day), items=items)
            sales.confirm_order(order)
            delivery = sales.post_delivery(order, delivery_date=day(order_day + 2), storage_location=self.store)
            sales.post_invoice(delivery, invoice_date=day(order_day + 3))

    def _open_before(self, kind, partner, cutoff):
        return [item for item in finance.open_items(self.company, reconciliation_type=kind, partner=partner)
                if item["posting_date"] < cutoff]

    def _pay_suppliers(self, when, month_start):
        """Pay last month's supplier invoices."""
        for supplier in self.suppliers:
            items = self._open_before(SUPPLIER, supplier, month_start)
            if items:
                finance.post_outgoing_payment(
                    company_code=self.company, supplier=supplier, payment_date=when, bank_account=self.bank,
                    amount=sum(i["open_amount"] for i in items),
                    allocations=[(i["line_id"], i["open_amount"]) for i in items], reference="EFT",
                )

    def _collect_from_customers(self, when, month_start):
        """Customers pay last month's invoices; slow payers only pay invoices older than two months."""
        for customer, late in self.customers:
            cutoff = (month_start - timedelta(days=1)).replace(day=1) if late else month_start
            items = self._open_before(CUSTOMER, customer, cutoff)
            if items:
                finance.post_incoming_payment(
                    company_code=self.company, customer=customer, payment_date=when, bank_account=self.bank,
                    amount=sum(i["open_amount"] for i in items),
                    allocations=[(i["line_id"], i["open_amount"]) for i in items], reference="EFT",
                )

    def _pay_sars(self, when):
        """Pay last month's PAYE, UIF and SDL to SARS (due on the 7th)."""
        run = self.previous_payroll
        if run is None:
            return
        lines = [(self._account(number), "D", amount) for number, amount in
                 (("2400", run.paye), ("2410", run.uif_employee + run.uif_employer), ("2420", run.sdl)) if amount]
        total = sum(amount for _, _, amount in lines)
        self._post("SARS EMP201 payment", when, lines + [(self.bank, "C", total)])

    def _settle_vat(self, when, month_start):
        """VAT201: pay SARS output VAT less input VAT up to the end of last month (or claim a refund)."""
        output_vat, input_vat = self._account("2100"), self._account("1300")
        totals = dict(JournalLine.objects.filter(
            company_code=self.company, gl_account__in=[output_vat, input_vat], posting_date__lt=month_start,
        ).values_list("gl_account").annotate(total=Sum("amount_local")))
        owed, claimable = -D(totals.get(output_vat.pk) or 0), D(totals.get(input_vat.pk) or 0)
        if not owed and not claimable:
            return
        lines = [(output_vat, "D", owed)] if owed else []
        lines += [(input_vat, "C", claimable)] if claimable else []
        net = owed - claimable
        if net:
            lines.append((self.bank, "C" if net > 0 else "D", abs(net)))
        self._post("SARS VAT201 settlement", when, lines)

    def _post(self, text, when, lines):
        finance.post_journal_entry(company_code=self.company, posting_date=when, header_text=text,
                                   lines=[finance.LineInput(account, side, amount) for account, side, amount in lines])
