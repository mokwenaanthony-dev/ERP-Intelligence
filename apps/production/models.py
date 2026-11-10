"""Production master data: where things are made, what they are made of and how.

ISA-95 equipment hierarchy (ADR 0006). The ERP owns it; the knowledge graph and the Unified Namespace
use the same codes::

    Company code (Enterprise) > Plant (Site) > ProductionArea (Area)
        > ProductionLine (ISA-95 work centre, type production line)
            > WorkCenter (SAP work centre = ISA-95 work unit, normally a work cell)

The SAP term "work centre" is kept because routings and costing use it, but each work centre also
records its ISA-95 level, and the graph labels it ``Cell``. Equipment codes are fixed once created:
they are graph keys and UNS topic segments (``uns_path``), so renaming one would break subscriptions.

Costing: an ``ActivityType`` (machine hours, labour hours) is recorded at work centres and valued at
the ``ActivityRate`` of the work centre's cost centre.

Recipes: a ``BillOfMaterials`` lists the components of a material in a plant; a ``Routing`` lists the
operations, each at a work centre with its activity quantities. Both are versioned by ``valid_from``
and never edited after creation: a change is a new version, and an old one can be deactivated.
"""
from decimal import Decimal

from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from apps.common.models import AuditedModel

# Upper-case letters, digits and hyphens. Lower-cased, the code is the UNS topic segment.
equipment_code = RegexValidator(
    r"^[A-Z0-9][A-Z0-9-]{0,9}$", "Use 1-10 upper-case letters, digits or hyphens, starting with a letter or digit."
)
POSITIVE = [MinValueValidator(Decimal("0.001"))]


def segment(code: str) -> str:
    """A code as a Unified Namespace topic segment: lower-case, never a display name."""
    return code.lower()


class ProductionArea(AuditedModel):
    """ISA-95 Area: a physical or logical part of a plant, e.g. assembly or packing."""

    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="production_areas")
    code = models.CharField(max_length=10, validators=[equipment_code])
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    isa95_level = "Area"

    class Meta:
        ordering = ["plant__code", "code"]
        constraints = [models.UniqueConstraint(fields=["plant", "code"], name="unique_area_per_plant")]

    @property
    def uns_path(self) -> str:
        """Topic path of the area: ``<company>/<plant>/<area>``."""
        return "/".join(segment(c) for c in (self.plant.company_code.code, self.plant.code, self.code))

    def __str__(self):
        return f"{self.plant.code}/{self.code} {self.name}"


class ProductionLine(AuditedModel):
    """ISA-95 work centre of type production line: equipment scheduled together as one line."""

    area = models.ForeignKey(ProductionArea, on_delete=models.PROTECT, related_name="lines")
    code = models.CharField(max_length=10, validators=[equipment_code],
                            help_text="Unique within the plant (checked by the service layer).")
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    isa95_level = "WorkCenter"
    isa95_type = "ProductionLine"

    class Meta:
        ordering = ["area__plant__code", "code"]
        constraints = [models.UniqueConstraint(fields=["area", "code"], name="unique_line_per_area")]

    @property
    def plant(self):
        """Lines belong to a plant through their area."""
        return self.area.plant

    @property
    def uns_path(self) -> str:
        """Topic path of the line: ``<company>/<plant>/<area>/<line>``."""
        return f"{self.area.uns_path}/{segment(self.code)}"

    def __str__(self):
        return f"{self.area.plant.code}/{self.code} {self.name}"


class ActivityType(AuditedModel):
    """A kind of work recorded at work centres and charged to production, e.g. machine hours.

    When a production order confirms activity (Wave 5b), the order is debited and the work centre's
    cost centre is credited on ``allocation_account``, so the cost centre is relieved of the cost it
    passed on.
    """

    company_code = models.ForeignKey("organization.CompanyCode", on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=6, validators=[equipment_code])
    name = models.CharField(max_length=100)
    unit = models.ForeignKey("masterdata.UnitOfMeasure", on_delete=models.PROTECT, related_name="+")
    allocation_account = models.ForeignKey(
        "finance.GLAccount", on_delete=models.PROTECT, related_name="+",
        help_text="Expense account credited on the cost centre when activity is charged to an order.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["company_code__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["company_code", "code"], name="unique_activity_type_per_company"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class ActivityRate(AuditedModel):
    """Price of one unit of an activity type at a cost centre, valid from a date (SAP KP26).

    The rate valid on a date is the one with the latest ``valid_from`` on or before it.
    """

    cost_center = models.ForeignKey("controlling.CostCenter", on_delete=models.PROTECT, related_name="activity_rates")
    activity_type = models.ForeignKey(ActivityType, on_delete=models.PROTECT, related_name="rates")
    valid_from = models.DateField()
    price = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))],
                                help_text="Per unit of the activity type, in company currency.")

    class Meta:
        ordering = ["cost_center__code", "activity_type__code", "-valid_from"]
        constraints = [
            models.UniqueConstraint(fields=["cost_center", "activity_type", "valid_from"],
                                    name="unique_activity_rate_per_date"),
        ]

    def __str__(self):
        return f"{self.cost_center.code}/{self.activity_type.code} from {self.valid_from}: {self.price}"


class WorkCenter(AuditedModel):
    """Where a routing operation is performed (SAP work centre), at the bottom of the hierarchy.

    In ISA-95 terms this is a work unit, normally a work cell. Its activity costs are charged through
    its cost centre, which must be a production cost centre of the plant's company code.
    """

    class ISA95Level(models.TextChoices):
        WORK_CELL = "WorkCell", "Work cell"
        UNIT = "Unit", "Unit (process industries)"
        STORAGE_UNIT = "StorageUnit", "Storage unit"

    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="work_centers")
    line = models.ForeignKey(ProductionLine, on_delete=models.PROTECT, related_name="work_centers")
    code = models.CharField(max_length=10, validators=[equipment_code])
    name = models.CharField(max_length=100)
    isa95_level = models.CharField(max_length=12, choices=ISA95Level.choices, default=ISA95Level.WORK_CELL)
    cost_center = models.ForeignKey("controlling.CostCenter", on_delete=models.PROTECT, related_name="work_centers")
    activity_types = models.ManyToManyField(ActivityType, related_name="work_centers", blank=True,
                                            help_text="Activities that routings may record here.")
    capacity_hours_per_day = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("8.00"),
                                                 validators=[MinValueValidator(Decimal("0.01"))])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["plant__code", "code"]
        constraints = [models.UniqueConstraint(fields=["plant", "code"], name="unique_work_center_per_plant")]

    @property
    def uns_path(self) -> str:
        """Topic path of the cell: ``<company>/<plant>/<area>/<line>/<cell>``."""
        return f"{self.line.uns_path}/{segment(self.code)}"

    def __str__(self):
        return f"{self.plant.code}/{self.code} {self.name}"


class _VersionedRecipe(AuditedModel):
    """Shared fields of BOMs and routings: one material in one plant, versioned by date."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    material = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="+")
    plant = models.ForeignKey("organization.Plant", on_delete=models.PROTECT, related_name="+")
    valid_from = models.DateField()
    base_quantity = models.DecimalField(max_digits=15, decimal_places=3, default=Decimal("1"), validators=POSITIVE,
                                        help_text="Quantity of the material the item quantities refer to.")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    description = models.CharField(max_length=100, blank=True)

    class Meta:
        abstract = True

    @property
    def key(self) -> str:
        """Business key, also the graph key: ``<plant>/<material>/<valid from>``."""
        return f"{self.plant.code}/{self.material.number}/{self.valid_from.isoformat()}"


class BillOfMaterials(_VersionedRecipe):
    """The components needed to make ``base_quantity`` of a material in a plant (SAP CS01)."""

    class Meta:
        ordering = ["plant__code", "material__number", "-valid_from"]
        verbose_name = "bill of materials"
        verbose_name_plural = "bills of materials"
        constraints = [
            models.UniqueConstraint(fields=["material", "plant", "valid_from"], name="unique_bom_version"),
        ]

    def __str__(self):
        return f"BOM {self.key}"


class BOMItem(models.Model):
    """One component of a BOM. Written once with the BOM; a change is a new BOM version."""

    bom = models.ForeignKey(BillOfMaterials, on_delete=models.CASCADE, related_name="items")
    item_number = models.PositiveIntegerField()
    component = models.ForeignKey("masterdata.Material", on_delete=models.PROTECT, related_name="used_in_boms")
    quantity = models.DecimalField(max_digits=15, decimal_places=3, validators=POSITIVE,
                                   help_text="Per base quantity of the BOM, in the component's base unit.")

    class Meta:
        ordering = ["bom", "item_number"]
        constraints = [models.UniqueConstraint(fields=["bom", "item_number"], name="unique_bom_item_number")]

    def __str__(self):
        return f"{self.bom} item {self.item_number}: {self.quantity} x {self.component.number}"


class Routing(_VersionedRecipe):
    """The operations that make a material in a plant, in order, each at a work centre (SAP CA01)."""

    class Meta:
        ordering = ["plant__code", "material__number", "-valid_from"]
        constraints = [
            models.UniqueConstraint(fields=["material", "plant", "valid_from"], name="unique_routing_version"),
        ]

    def __str__(self):
        return f"Routing {self.key}"


class RoutingOperation(models.Model):
    """One step of a routing, performed at a work centre of the routing's plant."""

    routing = models.ForeignKey(Routing, on_delete=models.CASCADE, related_name="operations")
    operation_number = models.PositiveIntegerField(help_text="Sequence, conventionally 10, 20, 30...")
    description = models.CharField(max_length=100)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, related_name="operations")

    class Meta:
        ordering = ["routing", "operation_number"]
        constraints = [
            models.UniqueConstraint(fields=["routing", "operation_number"], name="unique_operation_number"),
        ]

    def __str__(self):
        return f"{self.routing} op {self.operation_number:04d} at {self.work_center.code}"


class OperationActivity(models.Model):
    """Planned activity of an operation: a fixed setup quantity plus a quantity per base quantity.

    For an order of ``q`` units: activity = setup_quantity + quantity_per_base * q / base_quantity.
    """

    operation = models.ForeignKey(RoutingOperation, on_delete=models.CASCADE, related_name="activities")
    activity_type = models.ForeignKey(ActivityType, on_delete=models.PROTECT, related_name="+")
    setup_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"),
                                         validators=[MinValueValidator(Decimal("0"))])
    quantity_per_base = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"),
                                            validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        ordering = ["operation", "activity_type__code"]
        constraints = [
            models.UniqueConstraint(fields=["operation", "activity_type"], name="unique_activity_per_operation"),
        ]

    def __str__(self):
        return f"{self.operation}: {self.activity_type.code}"
