"""``manage.py setup_demo_production``: production master data for demo company 1000.

Run after ``setup_demo_company`` (``seed_data`` runs both). Creates, in plant 1000 Johannesburg:

* ISA-95 hierarchy: area ASSY > line L1 > work centres PRESS3 (press, a work cell) and ASSY1;
* production cost centre 4000 with activity types MACH (machine hours, R450) and LAB (labour, R220),
  allocated through G/L account 6950;
* a two-level product: FG-4001 Assembled bracket = 1 x SF-3001 Pressed frame + 4 x RM-2002 Fastener
  pack, where SF-3001 = 2.5 kg of RM-2001 Steel sheet; with routings at PRESS3 and ASSY1;
* opening stock of the raw materials and a sales price for FG-4001;
* production order accounts (Wave 5b): consumption 5400, factory output 5500, variances 5600.

The standard cost of 100 x FG-4001 is then R22,525.00 (R225.25 each); the tests check this figure.
Running it again changes nothing, except adding anything a later wave introduced (e.g. the order
accounts on a database set up with Wave 5a).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.controlling.models import CostCenter, ProfitCenter
from apps.finance.models import FiscalYear, GLAccount
from apps.inventory import services as inventory
from apps.inventory.models import AccountKey, InventoryAccountSetting, MovementType
from apps.masterdata.models import Material, UnitOfMeasure
from apps.organization.models import CompanyCode, Plant, SalesOrganization
from apps.production import services
from apps.production.models import ActivityRate, ActivityType, ProductionArea, ProductionLine, WorkCenter
from apps.sales.models import SalesPrice

MATERIALS = [
    ("RM-2001", "Steel sheet", Material.MaterialType.RAW, "KG"),
    ("RM-2002", "Fastener pack", Material.MaterialType.RAW, "EA"),
    ("SF-3001", "Pressed frame", Material.MaterialType.SEMI_FINISHED, "EA"),
    ("FG-4001", "Assembled bracket", Material.MaterialType.FINISHED, "EA"),
]
D = Decimal


class Command(BaseCommand):
    help = "Create demo production master data (equipment, activity rates, BOMs, routings) for company 1000."

    @transaction.atomic
    def handle(self, *args, **options):
        company = CompanyCode.objects.filter(code="1000").first()
        if company is None:
            raise CommandError("Demo company 1000 not found: run setup_demo_company first.")
        plant = Plant.objects.get(code="1000")
        self._order_accounts(company)
        if ProductionArea.objects.filter(plant=plant, code="ASSY").exists():
            self.stdout.write("Demo production data already exists.")
            return
        start = FiscalYear.objects.filter(company_code=company).order_by("start_date").first().start_date

        cost_center, activities = self._costing(company, start)
        work_centers = self._equipment(plant, cost_center, activities)
        materials = self._materials()
        self._recipes(plant, start, materials, work_centers, activities)
        self._opening_stock(plant, start, materials)
        SalesPrice.objects.get_or_create(material=materials["FG-4001"], valid_from=start,
                                         sales_organization=SalesOrganization.objects.get(code="1000"),
                                         defaults={"price": D("450.00")})
        estimate = services.cost_estimate(materials["FG-4001"], plant, D("100"), start)
        self.stdout.write(self.style.SUCCESS(
            f"Demo production ready: ASSY > L1 > PRESS3, ASSY1; FG-4001 standard cost "
            f"R{estimate['unit_cost']} each (R{estimate['total']} for 100)."
        ))

    def _order_accounts(self, company):
        """Where production orders post (Wave 5b), via inventory account determination."""
        chart = company.chart_of_accounts
        for number, name, key in (
            ("5400", "Consumption - production orders", AccountKey.CONSUMPTION),
            ("5500", "Factory output - production orders", AccountKey.FACTORY_OUTPUT),
            ("5600", "Production variances", AccountKey.PROD_VARIANCE),
        ):
            account, _ = GLAccount.objects.get_or_create(
                chart_of_accounts=chart, number=number,
                defaults={"name": name, "account_type": GLAccount.AccountType.EXPENSE},
            )
            InventoryAccountSetting.objects.get_or_create(chart_of_accounts=chart, account_key=key, material_type="",
                                                          defaults={"gl_account": account})

    def _costing(self, company, start):
        chart = company.chart_of_accounts
        allocation, _ = GLAccount.objects.get_or_create(
            chart_of_accounts=chart, number="6950",
            defaults={"name": "Production activities allocated", "account_type": GLAccount.AccountType.EXPENSE,
                      "cost_center_required": True},
        )
        cost_center, _ = CostCenter.objects.get_or_create(
            company_code=company, code="4000",
            defaults={"name": "Production - assembly", "category": CostCenter.Category.PRODUCTION,
                      "profit_center": ProfitCenter.objects.get(company_code=company, code="PC1000")},
        )
        hours = UnitOfMeasure.objects.get(code="HR")
        activities = {}
        for code, name, price in (("MACH", "Machine hours", "450.00"), ("LAB", "Labour hours", "220.00")):
            activities[code], _ = ActivityType.objects.get_or_create(
                company_code=company, code=code,
                defaults={"name": name, "unit": hours, "allocation_account": allocation},
            )
            ActivityRate.objects.get_or_create(cost_center=cost_center, activity_type=activities[code],
                                               valid_from=start, defaults={"price": D(price)})
        return cost_center, activities

    def _equipment(self, plant, cost_center, activities):
        area = ProductionArea.objects.create(plant=plant, code="ASSY", name="Assembly")
        line = ProductionLine.objects.create(area=area, code="L1", name="Line 1")
        work_centers = {}
        for code, name, kinds in (("PRESS3", "Press 3", ("MACH", "LAB")), ("ASSY1", "Assembly cell 1", ("LAB",))):
            services.check_work_center(plant, line, cost_center, [activities[k] for k in kinds])
            work_centers[code] = WorkCenter.objects.create(plant=plant, line=line, code=code, name=name,
                                                           cost_center=cost_center)
            work_centers[code].activity_types.set([activities[k] for k in kinds])
        return work_centers

    def _materials(self):
        materials = {}
        for number, description, material_type, unit in MATERIALS:
            materials[number], _ = Material.objects.get_or_create(
                number=number, defaults={"description": description, "material_type": material_type,
                                         "base_unit": UnitOfMeasure.objects.get(code=unit)},
            )
        return materials

    def _recipes(self, plant, start, m, wc, act):
        services.create_bom(material=m["SF-3001"], plant=plant, valid_from=start, description="Pressed frame",
                            items=[services.ComponentInput(m["RM-2001"], D("2.5"))])
        services.create_bom(material=m["FG-4001"], plant=plant, valid_from=start, description="Assembled bracket",
                            items=[services.ComponentInput(m["SF-3001"], D("1")),
                                   services.ComponentInput(m["RM-2002"], D("4"))])
        services.create_routing(material=m["SF-3001"], plant=plant, valid_from=start, operations=[
            services.OperationInput("Press frame", wc["PRESS3"], [
                services.ActivityInput(act["MACH"], setup_quantity=D("0.5"), quantity_per_base=D("0.1")),
                services.ActivityInput(act["LAB"], quantity_per_base=D("0.1")),
            ]),
        ])
        services.create_routing(material=m["FG-4001"], plant=plant, valid_from=start, operations=[
            services.OperationInput("Assemble bracket", wc["ASSY1"], [
                services.ActivityInput(act["LAB"], quantity_per_base=D("0.25")),
            ]),
        ])

    def _opening_stock(self, plant, start, m):
        storage_location = plant.storage_locations.get(code="0001")
        inventory.post_goods_movement(
            movement_type=MovementType.INITIAL_ENTRY, plant=plant, posting_date=start, reference="OPENING-PP",
            header_text="Opening stock, raw materials", items=[
                inventory.ItemInput(m["RM-2001"], storage_location, D("1000"), unit_price=D("38.00")),
                inventory.ItemInput(m["RM-2002"], storage_location, D("2000"), unit_price=D("1.50")),
            ],
        )
