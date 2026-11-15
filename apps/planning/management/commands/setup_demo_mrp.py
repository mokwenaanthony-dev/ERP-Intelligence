"""``manage.py setup_demo_mrp``: planning parameters and a customer order for the demo product (Wave 8b).

Run after ``setup_demo_production`` (``seed_data`` runs it). For plant 1000:

* FG-4001 Assembled bracket: made in lots of 50, safety stock 10, 5 days lead time;
* SF-3001 Pressed frame: made lot for lot, 3 days;
* RM-2001 Steel sheet: bought in lots of at least 500 kg from Highveld Supplies CC, 10 days;
* RM-2002 Fastener pack: bought lot for lot with a safety stock of 200, 14 days;
* a sales order from Acme Retail for 450 x FG-4001, wanted in the middle of next month.

Then ``python manage.py run_mrp --plant 1000`` plans 500 brackets (one lot of 50 at once, late, to
restore the safety stock, and nine lots for the order), 500 frames for them, 500 kg of steel (1,250 kg
needed, 1,000 in stock, minimum lot 500) and 200 fastener packs (2,000 needed and in stock, but the
safety stock of 200 must stay). Running this command again changes nothing.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.masterdata.models import BusinessPartner, Material
from apps.organization.models import Plant, SalesOrganization
from apps.planning.models import MaterialPlanning
from apps.planning.services import add_months, month_start
from apps.sales import services as sales
from apps.sales.models import SalesOrder

D = Decimal
MAKE, BUY = MaterialPlanning.Procurement.MAKE, MaterialPlanning.Procurement.BUY
LOT = MaterialPlanning.LotSize
PARAMETERS = (  # material, procurement, lot size, lot quantity, safety stock, lead days, preferred supplier
    ("FG-4001", MAKE, LOT.FIXED, D("50"), D("10"), 5, None),
    ("SF-3001", MAKE, LOT.LOT_FOR_LOT, None, D("0"), 3, None),
    ("RM-2001", BUY, LOT.MINIMUM, D("500"), D("0"), 10, "Highveld Supplies CC"),
    ("RM-2002", BUY, LOT.LOT_FOR_LOT, None, D("200"), 14, None),
)
REFERENCE = "DEMO-MRP"


class Command(BaseCommand):
    help = "Create demo MRP parameters and a sales order for FG-4001 in plant 1000."

    @transaction.atomic
    def handle(self, *args, **options):
        plant = Plant.objects.filter(code="1000").select_related("company_code").first()
        if plant is None or not Material.objects.filter(number="FG-4001").exists():
            raise CommandError("Run setup_demo_company and setup_demo_production first.")
        created = 0
        for number, procurement, lot_size, lot_quantity, safety, lead, supplier in PARAMETERS:
            _, new = MaterialPlanning.objects.get_or_create(
                material=Material.objects.get(number=number), plant=plant,
                defaults={"procurement": procurement, "lot_size": lot_size, "lot_quantity": lot_quantity,
                          "safety_stock": safety, "lead_time_days": lead,
                          "preferred_supplier": BusinessPartner.objects.filter(name=supplier).first()})
            created += new
        order = None
        if not SalesOrder.objects.filter(customer_reference=REFERENCE).exists():
            today = timezone.localdate()
            order = sales.create_sales_order(
                sales_organization=SalesOrganization.objects.get(code="1000"),
                customer=BusinessPartner.objects.get(name="Acme Retail (Pty) Ltd"), order_date=today,
                requested_delivery_date=add_months(month_start(today), 1) + timedelta(days=14),
                customer_reference=REFERENCE,
                items=[sales.OrderItemInput(material=Material.objects.get(number="FG-4001"), quantity=D("450"),
                                            plant=plant)])
        if not created and order is None:
            self.stdout.write("Demo MRP data already exists: nothing to do.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"Demo MRP: planning parameters for {created} material(s)"
            + (f", sales order {order.order_number} for 450 x FG-4001" if order else "")
            + ". Plan it with: python manage.py run_mrp --plant 1000"))
