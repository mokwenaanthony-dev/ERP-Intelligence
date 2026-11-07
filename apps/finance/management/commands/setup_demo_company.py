"""Create a ready-to-use South African demo company.

Usage:
    python manage.py setup_demo_company
    python manage.py setup_demo_company --fiscal-year 2027 --start-month 3

Creates currencies and exchange rates, units of measure, chart of accounts
"CAZA" with VAT tax codes, company code 1000 (ZAR) with a plant, storage
location and sales/purchasing organisations, a fiscal year with 12 open
periods, sample business partners and materials, two sample postings (opening
capital and a customer invoice with 15% VAT), inventory account determination
opening stock in plant 1000 (posted to the journal as well), sales prices,
revenue account determination, the purchasing settings (input VAT, price
difference account, 5% invoice price tolerance), profit and cost centres, an
org chart with three employees and the payroll settings and SARS PAYE table.

Safe to run more than once: existing records are left alone.
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.finance import services
from apps.finance.models import ChartOfAccounts, DocumentType, FiscalYear, GLAccount, JournalEntry, TaxCode
from apps.inventory import services as inventory
from apps.inventory.models import AccountKey, InventoryAccountSetting, MaterialDocument, MovementType
from apps.masterdata.models import (
    BusinessPartner,
    Currency,
    CustomerRole,
    ExchangeRate,
    Material,
    SupplierRole,
    UnitOfMeasure,
)
from apps.organization.models import (
    CompanyCode,
    Plant,
    PurchasingOrganization,
    SalesOrganization,
    StorageLocation,
)
from apps.controlling.models import CostCenter, PlantProfitCenter, ProfitCenter
from apps.hr import services as hr
from apps.hr.models import Employee, OrgUnit, PayeBracket, PayeTable, PayrollSetting, Position
from apps.purchasing.models import PurchasingAccountSetting
from apps.sales.models import RevenueAccountSetting, SalesPrice

ASSET, LIABILITY, EQUITY = GLAccount.AccountType.ASSET, GLAccount.AccountType.LIABILITY, GLAccount.AccountType.EQUITY
REVENUE, EXPENSE = GLAccount.AccountType.REVENUE, GLAccount.AccountType.EXPENSE
CUSTOMER, SUPPLIER = GLAccount.ReconciliationType.CUSTOMER, GLAccount.ReconciliationType.SUPPLIER

CURRENCIES = [("ZAR", "South African rand"), ("USD", "US dollar"), ("EUR", "Euro"), ("GBP", "Pound sterling")]
UNITS = [("EA", "Each"), ("KG", "Kilogram"), ("L", "Litre"), ("HR", "Hour"), ("BOX", "Box")]

# number, name, type, reconciliation type
ACCOUNTS = [
    ("1000", "Bank - current account", ASSET, None),
    ("1100", "Accounts receivable - trade", ASSET, CUSTOMER),
    ("1200", "Inventory - trading goods", ASSET, None),
    ("1300", "VAT input", ASSET, None),
    ("2000", "Accounts payable - trade", LIABILITY, SUPPLIER),
    ("2100", "VAT output", LIABILITY, None),
    ("2200", "Accrued expenses", LIABILITY, None),
    ("2300", "Goods received / invoice received clearing", LIABILITY, None),
    ("2400", "PAYE payable", LIABILITY, None),
    ("2410", "UIF payable", LIABILITY, None),
    ("2420", "SDL payable", LIABILITY, None),
    ("2500", "Salaries payable", LIABILITY, None),
    ("3000", "Share capital", EQUITY, None),
    ("3100", "Retained earnings", EQUITY, None),
    ("3200", "Inventory opening balances", EQUITY, None),
    ("4000", "Sales revenue", REVENUE, None),
    ("4100", "Service revenue", REVENUE, None),
    ("5000", "Cost of sales", EXPENSE, None),
    ("5100", "Inventory scrapping", EXPENSE, None),
    ("5200", "Inventory differences", EXPENSE, None),
    ("5300", "Purchase price differences", EXPENSE, None),
    ("6000", "Salaries and wages", EXPENSE, None),
    ("6010", "UIF employer contributions", EXPENSE, None),
    ("6020", "Skills development levy", EXPENSE, None),
    ("6100", "Rent", EXPENSE, None),
    ("6200", "Utilities", EXPENSE, None),
    ("6900", "Bank charges", EXPENSE, None),
    ("7000", "Foreign exchange differences", EXPENSE, None),
]

# code, description, type, rate, account
TAX_CODES = [
    ("O1", "Output VAT 15%", TaxCode.TaxType.OUTPUT, Decimal("15.00"), "2100"),
    ("O0", "Output VAT zero-rated", TaxCode.TaxType.OUTPUT, Decimal("0.00"), None),
    ("OE", "Output exempt supplies", TaxCode.TaxType.OUTPUT, Decimal("0.00"), None),
    ("I1", "Input VAT 15%", TaxCode.TaxType.INPUT, Decimal("15.00"), "1300"),
    ("I0", "Input VAT zero-rated", TaxCode.TaxType.INPUT, Decimal("0.00"), None),
]


# account key -> account number (all material types)
# Payroll accounts must always carry a cost centre.
COST_CENTER_REQUIRED = ("6000", "6010", "6020")

# SARS rates of tax for individuals, 2027 year of assessment (1 March 2026 - 28 February 2027):
# (taxable income above, base tax, rate %). Source: sars.gov.za "Rates of tax for individuals".
# Check SARS each year and load the new table before running payroll for March.
PAYE_TABLES = {
    2027: (Decimal("17820"), [
        ("0", "0", "18"), ("245100", "44118", "26"), ("383100", "79998", "31"), ("530200", "125599", "36"),
        ("695800", "185215", "39"), ("887000", "259783", "41"), ("1878600", "666339", "45"),
    ]),
}

# code, name, profit centre / category
PROFIT_CENTERS = [("PC1000", "Trading"), ("PC9000", "Corporate")]
COST_CENTERS = [
    ("1000", "Management", "PC9000", CostCenter.Category.MANAGEMENT),
    ("2000", "Sales", "PC1000", CostCenter.Category.SALES),
    ("3000", "Warehouse", "PC1000", CostCenter.Category.PRODUCTION),
]
# org unit code, name, cost centre, position code, title, employee first, last name, monthly salary
ORG_CHART = [
    ("EXEC", "Executive", "1000", "MD", "Managing director", "Thandi", "Nkosi", "85000.00"),
    ("SALES", "Sales", "2000", "SREP", "Sales representative", "Sipho", "Dlamini", "32000.00"),
    ("WH", "Warehouse", "3000", "WSUP", "Warehouse supervisor", "Lerato", "Mokoena", "18500.00"),
]

INVENTORY_ACCOUNTS = {
    AccountKey.INVENTORY: "1200",
    AccountKey.GR_IR: "2300",
    AccountKey.INITIAL_ENTRY: "3200",
    AccountKey.COST_OF_SALES: "5000",
    AccountKey.SCRAP: "5100",
    AccountKey.DIFFERENCES: "5200",
}


class Command(BaseCommand):
    help = "Create a South African demo company with finance configuration and sample postings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fiscal-year", type=int, help="Fiscal year to create (default: the one containing today)."
        )
        parser.add_argument("--start-month", type=int, default=3, help="Fiscal year start month (default 3 = March).")

    @transaction.atomic
    def handle(self, *args, fiscal_year=None, start_month=3, **options):
        currencies = {
            code: Currency.objects.get_or_create(code=code, defaults={"name": name})[0] for code, name in CURRENCIES
        }
        units = {
            code: UnitOfMeasure.objects.get_or_create(code=code, defaults={"name": name})[0] for code, name in UNITS
        }

        chart, _ = ChartOfAccounts.objects.get_or_create(
            code="CAZA", defaults={"name": "Chart of accounts - South Africa"}
        )
        accounts = {}
        for number, name, account_type, recon in ACCOUNTS:
            accounts[number], _ = GLAccount.objects.get_or_create(
                chart_of_accounts=chart, number=number,
                defaults={"name": name, "account_type": account_type,
                          "reconciliation_type": recon or GLAccount.ReconciliationType.NONE},
            )
        GLAccount.objects.filter(chart_of_accounts=chart, number__in=COST_CENTER_REQUIRED).update(
            cost_center_required=True
        )
        for code, description, tax_type, rate, account in TAX_CODES:
            TaxCode.objects.get_or_create(
                chart_of_accounts=chart, code=code,
                defaults={"description": description, "tax_type": tax_type, "rate": rate,
                          "gl_account": accounts[account] if account else None},
            )

        company, created = CompanyCode.objects.get_or_create(
            code="1000",
            defaults={"name": "ERP Demo (Pty) Ltd", "currency": currencies["ZAR"], "chart_of_accounts": chart,
                      "vat_registration_number": "4000000000", "fiscal_year_start_month": start_month},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created company code {company}"))
        plant, _ = Plant.objects.get_or_create(
            code="1000", defaults={"name": "Johannesburg", "company_code": company, "address": "Johannesburg, Gauteng"}
        )
        main_store, _ = StorageLocation.objects.get_or_create(
            plant=plant, code="0001", defaults={"name": "Main warehouse"}
        )
        StorageLocation.objects.get_or_create(plant=plant, code="0002", defaults={"name": "Dispatch area"})
        for key, number in INVENTORY_ACCOUNTS.items():
            InventoryAccountSetting.objects.get_or_create(
                chart_of_accounts=chart, account_key=key, material_type="", defaults={"gl_account": accounts[number]}
            )
        sales_org, _ = SalesOrganization.objects.get_or_create(
            code="1000", defaults={"name": "Sales South Africa", "company_code": company}
        )
        output_vat = TaxCode.objects.get(chart_of_accounts=chart, code="O1")
        for material_type, account in (("", "4000"), ("DIEN", "4100")):
            RevenueAccountSetting.objects.get_or_create(
                chart_of_accounts=chart, material_type=material_type,
                defaults={"revenue_account": accounts[account], "default_tax_code": output_vat},
            )
        PurchasingOrganization.objects.get_or_create(
            code="1000", defaults={"name": "Purchasing South Africa", "company_code": company}
        )
        PurchasingAccountSetting.objects.get_or_create(
            chart_of_accounts=chart,
            defaults={"default_tax_code": TaxCode.objects.get(chart_of_accounts=chart, code="I1"),
                      "price_difference_account": accounts["5300"], "price_tolerance_percent": Decimal("5.00")},
        )

        self._controlling_and_hr(company, plant, accounts)

        today = date.today()
        year = fiscal_year or self._fiscal_year_containing(today, company.fiscal_year_start_month)
        fy = FiscalYear.objects.filter(company_code=company, year=year).first()
        if fy is None:
            fy = services.create_fiscal_year(company, year)
            self.stdout.write(self.style.SUCCESS(f"Created fiscal year {fy} ({fy.start_date} to {fy.end_date})"))

        for code, rate in (("USD", "18.000000"), ("EUR", "19.500000"), ("GBP", "22.800000")):
            ExchangeRate.objects.get_or_create(
                from_currency=currencies[code], to_currency=currencies["ZAR"], valid_from=fy.start_date,
                defaults={"rate": Decimal(rate)},
            )

        customer = self._partner("Acme Retail (Pty) Ltd", "4111111111")
        CustomerRole.objects.get_or_create(
            business_partner=customer, company_code=company, defaults={"reconciliation_account": accounts["1100"]}
        )
        supplier = self._partner("Highveld Supplies CC", "4222222222")
        SupplierRole.objects.get_or_create(
            business_partner=supplier, company_code=company, defaults={"reconciliation_account": accounts["2000"]}
        )
        for number, description, price in (("TG-1001", "Standard widget", "199.00"),
                                           ("TG-1002", "Deluxe gadget", "499.00")):
            material, _ = Material.objects.get_or_create(
                number=number, defaults={"description": description, "base_unit": units["EA"],
                                         "material_type": Material.MaterialType.TRADING_GOODS},
            )
            SalesPrice.objects.get_or_create(
                material=material, sales_organization=sales_org, valid_from=fy.start_date,
                defaults={"price": Decimal(price)},
            )

        if not JournalEntry.objects.filter(company_code=company, fiscal_year=year).exists():
            self._sample_postings(company, fy, accounts, customer)
        if not MaterialDocument.objects.filter(company_code=company).exists():
            self._opening_stock(plant, main_store, fy)

        self.stdout.write(self.style.SUCCESS(
            f"Demo company {company.code} ready: fiscal year {year}, chart {chart.code}, currency {company.currency}."
        ))

    def _controlling_and_hr(self, company, plant, accounts):
        profit_centers = {
            code: ProfitCenter.objects.get_or_create(company_code=company, code=code, defaults={"name": name})[0]
            for code, name in PROFIT_CENTERS
        }
        cost_centers = {
            code: CostCenter.objects.get_or_create(
                company_code=company, code=code,
                defaults={"name": name, "profit_center": profit_centers[pc], "category": category},
            )[0]
            for code, name, pc, category in COST_CENTERS
        }
        PlantProfitCenter.objects.get_or_create(plant=plant, defaults={"profit_center": profit_centers["PC1000"]})

        for tax_year, (rebate, brackets) in PAYE_TABLES.items():
            table, created = PayeTable.objects.get_or_create(tax_year=tax_year, defaults={"primary_rebate": rebate})
            if created:
                PayeBracket.objects.bulk_create([
                    PayeBracket(table=table, lower_limit=Decimal(low), base_tax=Decimal(base), rate=Decimal(rate))
                    for low, base, rate in brackets
                ])
        PayrollSetting.objects.get_or_create(company_code=company, defaults={
            "salary_account": accounts["6000"], "uif_expense_account": accounts["6010"],
            "sdl_expense_account": accounts["6020"], "paye_payable_account": accounts["2400"],
            "uif_payable_account": accounts["2410"], "sdl_payable_account": accounts["2420"],
            "net_pay_account": accounts["2500"],
        })
        if Employee.objects.filter(company_code=company).exists():
            return
        for unit_code, unit_name, cc, position_code, title, first, last, salary in ORG_CHART:
            unit, _ = OrgUnit.objects.get_or_create(
                company_code=company, code=unit_code, defaults={"name": unit_name, "cost_center": cost_centers[cc]}
            )
            position, _ = Position.objects.get_or_create(org_unit=unit, code=position_code, defaults={"title": title})
            hr.create_employee(company_code=company, position=position, first_name=first, last_name=last,
                               hire_date=date(2024, 3, 1), monthly_salary=Decimal(salary))
        self.stdout.write(self.style.SUCCESS(
            "Created profit centres, cost centres, org units, positions and 3 employees"
        ))

    @staticmethod
    def _fiscal_year_containing(day: date, start_month: int) -> int:
        if start_month == 1:
            return day.year
        return day.year + 1 if day.month >= start_month else day.year

    @staticmethod
    def _partner(name, vat_number):
        partner = BusinessPartner.objects.filter(name=name).first()
        return partner or BusinessPartner.objects.create(name=name, vat_number=vat_number, city="Johannesburg")

    def _sample_postings(self, company, fiscal_year, accounts, customer):
        line = services.LineInput
        start = fiscal_year.start_date
        services.post_journal_entry(
            company_code=company, posting_date=start, header_text="Opening share capital",
            lines=[
                line(accounts["1000"], "D", Decimal("500000.00")),
                line(accounts["3000"], "C", Decimal("500000.00")),
            ],
        )
        output_vat = TaxCode.objects.get(chart_of_accounts=company.chart_of_accounts, code="O1")
        services.post_journal_entry(
            company_code=company, document_type=DocumentType.CUSTOMER_INVOICE, posting_date=start,
            reference="INV-0001", header_text="Sample customer invoice",
            lines=[
                line(accounts["1100"], "D", Decimal("11500.00"), business_partner=customer),
                line(accounts["4000"], "C", Decimal("10000.00"), tax_code=output_vat),  # VAT line added automatically
            ],
        )
        self.stdout.write(self.style.SUCCESS("Posted 2 sample journal entries"))

    def _opening_stock(self, plant, storage_location, fiscal_year):
        items = [
            inventory.ItemInput(Material.objects.get(number="TG-1001"), storage_location, Decimal("100"),
                                unit_price=Decimal("120.00")),
            inventory.ItemInput(Material.objects.get(number="TG-1002"), storage_location, Decimal("50"),
                                unit_price=Decimal("300.00")),
        ]
        inventory.post_goods_movement(
            movement_type=MovementType.INITIAL_ENTRY, plant=plant, posting_date=fiscal_year.start_date,
            reference="OPENING", header_text="Opening stock", items=items,
        )
        self.stdout.write(self.style.SUCCESS("Posted opening stock: 100 x TG-1001, 50 x TG-1002 (R27,000.00)"))
