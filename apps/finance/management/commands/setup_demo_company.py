"""Create a ready-to-use South African demo company.

Usage:
    python manage.py setup_demo_company
    python manage.py setup_demo_company --fiscal-year 2027 --start-month 3

Creates currencies and exchange rates, units of measure, chart of accounts
"CAZA" with VAT tax codes, company code 1000 (ZAR) with a plant, storage
location and sales/purchasing organisations, a fiscal year with 12 open
periods, sample business partners and materials, and two sample postings
(opening capital and a customer invoice with 15% VAT).

Safe to run more than once: existing records are left alone.
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.finance import services
from apps.finance.models import ChartOfAccounts, DocumentType, FiscalYear, GLAccount, JournalEntry, TaxCode
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
    ("3000", "Share capital", EQUITY, None),
    ("3100", "Retained earnings", EQUITY, None),
    ("4000", "Sales revenue", REVENUE, None),
    ("4100", "Service revenue", REVENUE, None),
    ("5000", "Cost of sales", EXPENSE, None),
    ("6000", "Salaries and wages", EXPENSE, None),
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
        StorageLocation.objects.get_or_create(plant=plant, code="0001", defaults={"name": "Main warehouse"})
        SalesOrganization.objects.get_or_create(
            code="1000", defaults={"name": "Sales South Africa", "company_code": company}
        )
        PurchasingOrganization.objects.get_or_create(
            code="1000", defaults={"name": "Purchasing South Africa", "company_code": company}
        )

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
        for number, description in (("TG-1001", "Standard widget"), ("TG-1002", "Deluxe gadget")):
            Material.objects.get_or_create(
                number=number, defaults={"description": description, "base_unit": units["EA"],
                                         "material_type": Material.MaterialType.TRADING_GOODS},
            )

        if not JournalEntry.objects.filter(company_code=company, fiscal_year=year).exists():
            self._sample_postings(company, fy, accounts, customer)

        self.stdout.write(self.style.SUCCESS(
            f"Demo company {company.code} ready: fiscal year {year}, chart {chart.code}, currency {company.currency}."
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
