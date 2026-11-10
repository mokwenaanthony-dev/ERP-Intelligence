"""Production master data (Wave 5a): ISA-95 equipment hierarchy, activity rates, BOMs, routings and the
standard cost estimate, through the services and the API, plus their projection into the graph.

The demo product: FG-4001 Assembled bracket = 1 x SF-3001 Pressed frame + 4 x RM-2002 (R1.50 each);
SF-3001 = 2.5 kg RM-2001 (R38/kg), pressed at PRESS3 (machine R450/h, labour R220/h); assembly at
ASSY1. Standard cost of 100 brackets, worked out by hand:

    SF-3001 x 100 (no stock, rolled up): steel 250 kg x 38 = 9,500.00
        machine 0.5 setup + 0.1 x 100 = 10.5 h x 450 = 4,725.00; labour 10 h x 220 = 2,200.00 -> 16,425.00
    RM-2002 x 400 x 1.50 = 600.00
    Assembly labour 0.25 x 100 = 25 h x 220 = 5,500.00
    Total 22,525.00 -> 225.25 per bracket
"""
import logging
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import Role
from apps.controlling.models import CostCenter
from apps.graph import access, processor
from apps.graph.models import OutboxEvent
from apps.graph.store import MemoryStore
from apps.masterdata.models import Material, UnitOfMeasure
from apps.organization.models import Plant
from apps.production import services
from apps.production.models import ActivityType, BillOfMaterials, ProductionArea, ProductionLine, WorkCenter

pytestmark = pytest.mark.django_db
D = Decimal
START = date(2026, 3, 1)
API = "/api/production"


@pytest.fixture
def pp(demo):
    """Demo company plus demo production master data."""
    call_command("setup_demo_production", stdout=StringIO())
    demo.plant = Plant.objects.get(code="1000")
    demo.material = lambda number: Material.objects.get(number=number)
    demo.wc = lambda code: WorkCenter.objects.get(code=code)
    demo.activity = lambda code: ActivityType.objects.get(code=code)
    return demo


@pytest.fixture
def planner(client_for):
    return client_for(Role.PRODUCTION_PLANNER)


def new_material(number, material_type=Material.MaterialType.RAW, unit="EA"):
    return Material.objects.create(number=number, description=number, material_type=material_type,
                                   base_unit=UnitOfMeasure.objects.get(code=unit))


# --- demo data and command -------------------------------------------------------------------------

def test_demo_command_is_idempotent_and_needs_the_company(pp, db):
    out = StringIO()
    call_command("setup_demo_production", stdout=out)
    assert "already exists" in out.getvalue()
    assert WorkCenter.objects.count() == 2


def test_demo_command_without_company(db):
    with pytest.raises(CommandError, match="setup_demo_company"):
        call_command("setup_demo_production", stdout=StringIO())


# --- equipment hierarchy ---------------------------------------------------------------------------

def test_uns_paths_follow_the_isa95_hierarchy(pp):
    press = pp.wc("PRESS3")
    assert press.uns_path == "1000/1000/assy/l1/press3"
    assert press.line.uns_path == "1000/1000/assy/l1"
    assert press.line.area.uns_path == "1000/1000/assy"
    assert (press.isa95_level, press.line.isa95_level, press.line.isa95_type) == ("WorkCell", "WorkCenter",
                                                                                  "ProductionLine")


def test_planner_builds_the_hierarchy_through_the_api(pp, planner):
    area = planner.post(f"{API}/areas/", {"plant": "1000", "code": "PACK", "name": "Packing"}, format="json")
    assert area.status_code == 201, area.data
    assert area.data["uns_path"] == "1000/1000/pack"
    line = planner.post(f"{API}/lines/", {"area": area.data["id"], "code": "P1", "name": "Pack line"}, format="json")
    assert line.status_code == 201, line.data
    cell = planner.post(f"{API}/work-centers/", {
        "plant": "1000", "line": line.data["id"], "code": "WRAP1", "name": "Wrapper", "cost_center": "4000",
        "activity_types": ["LAB"],
    }, format="json")
    assert cell.status_code == 201, cell.data
    assert cell.data["uns_path"] == "1000/1000/pack/p1/wrap1"
    assert cell.data["activity_types"] == ["LAB"]


def test_codes_cannot_change_but_names_can(pp, planner):
    area = ProductionArea.objects.get(code="ASSY")
    response = planner.patch(f"{API}/areas/{area.pk}/", {"code": "ASM"}, format="json")
    assert response.status_code == 400
    assert "UNS topic paths" in str(response.data["code"])
    response = planner.patch(f"{API}/areas/{area.pk}/", {"name": "Final assembly"}, format="json")
    assert response.status_code == 200 and response.data["name"] == "Final assembly"


def test_line_codes_are_unique_per_plant(pp, planner):
    other = ProductionArea.objects.create(plant=pp.plant, code="PACK", name="Packing")
    response = planner.post(f"{API}/lines/", {"area": other.pk, "code": "L1", "name": "Duplicate"}, format="json")
    assert response.status_code == 400
    assert "Line L1 already exists in plant 1000" in str(response.data)


def test_work_center_needs_a_production_cost_center(pp, planner, caplog):
    line = ProductionLine.objects.get(code="L1")
    payload = {"plant": "1000", "line": line.pk, "code": "QC1", "name": "Inspection", "cost_center": "2000"}
    with caplog.at_level(logging.WARNING, logger="erp.api"):
        response = planner.post(f"{API}/work-centers/", payload, format="json")
    assert response.status_code == 400
    assert "not a production cost centre" in str(response.data)
    # The rejection is logged once, with the line in services.py that raised it.
    warnings = [r.getMessage() for r in caplog.records if r.name == "erp.api"]
    assert len(warnings) == 1 and "raised at apps/production/services.py:" in warnings[0]
    payload["cost_center"] = "9999"
    assert "not found" in str(planner.post(f"{API}/work-centers/", payload, format="json").data)


def test_work_center_rules_in_the_service(pp):
    line = ProductionLine.objects.get(code="L1")
    cost_center = CostCenter.objects.get(code="4000")
    cost_center.is_active = False
    with pytest.raises(services.ProductionError, match="inactive"):
        services.check_work_center(pp.plant, line, cost_center)
    other_plant = Plant.objects.create(code="2000", name="Durban", company_code=pp.company)
    with pytest.raises(services.ProductionError, match="not in plant 2000"):
        services.check_work_center(other_plant, line, CostCenter.objects.get(code="4000"))


def test_equipment_tree(pp, client_for):
    viewer = client_for(Role.VIEWER)
    response = viewer.get(f"{API}/equipment/?plant=1000")
    assert response.status_code == 200
    tree = response.data
    assert (tree["enterprise"], tree["site"], tree["uns_path"]) == ("1000", "1000", "1000/1000")
    line = tree["areas"][0]["lines"][0]
    assert [c["code"] for c in line["work_centers"]] == ["ASSY1", "PRESS3"]
    assert line["work_centers"][1] == {
        "code": "PRESS3", "name": "Press 3", "isa95_level": "WorkCell", "cost_center": "4000",
        "activity_types": ["LAB", "MACH"], "is_active": True, "uns_path": "1000/1000/assy/l1/press3",
    }
    assert viewer.get(f"{API}/equipment/?plant=XX").status_code == 400


def test_roles(pp, client_for):
    viewer, finance = client_for(Role.VIEWER), client_for(Role.FINANCE_MANAGER)
    assert viewer.get(f"{API}/work-centers/").status_code == 200
    assert viewer.post(f"{API}/areas/", {"plant": "1000", "code": "X", "name": "X"}).status_code == 403
    assert finance.post(f"{API}/areas/", {"plant": "1000", "code": "X", "name": "X"}).status_code == 403
    assert client_for(Role.SALES_REP).get(f"{API}/cost-estimate/?material=FG-4001&plant=1000").status_code == 403


# --- activity types and rates ----------------------------------------------------------------------

def test_finance_manager_maintains_activity_types_and_rates(pp, client_for, planner):
    finance = client_for(Role.FINANCE_MANAGER)
    payload = {"company_code": "1000", "code": "ENGY", "name": "Energy", "unit": "HR", "allocation_account": "6950"}
    assert planner.post(f"{API}/activity-types/", payload, format="json").status_code == 403
    created = finance.post(f"{API}/activity-types/", payload, format="json")
    assert created.status_code == 201, created.data
    bad = finance.post(f"{API}/activity-types/", {**payload, "code": "X", "allocation_account": "1000"}, format="json")
    assert "expense account" in str(bad.data)
    missing = finance.post(f"{API}/activity-types/", {**payload, "code": "Y", "allocation_account": "0000"},
                           format="json")
    assert "not found" in str(missing.data)

    cost_center = CostCenter.objects.get(code="4000")
    rate = finance.post(f"{API}/activity-rates/", {
        "cost_center": cost_center.pk, "activity_type": created.data["id"], "valid_from": "2026-03-01",
        "price": "95.00",
    }, format="json")
    assert rate.status_code == 201, rate.data
    assert services.activity_rate(cost_center, ActivityType.objects.get(code="ENGY"), date(2026, 6, 1)) == D("95.00")


def test_the_latest_rate_on_or_before_the_date_applies(pp):
    cost_center, machine = CostCenter.objects.get(code="4000"), pp.activity("MACH")
    machine.rates.create(cost_center=cost_center, valid_from=date(2026, 9, 1), price=D("500.00"))
    assert services.activity_rate(cost_center, machine, date(2026, 8, 31)) == D("450.00")
    assert services.activity_rate(cost_center, machine, date(2026, 9, 1)) == D("500.00")
    with pytest.raises(services.ProductionError, match="No rate for activity MACH"):
        services.activity_rate(cost_center, machine, date(2026, 2, 1))


# --- bills of materials ----------------------------------------------------------------------------

def test_create_bom_through_the_api(pp, planner):
    gadget = new_material("FG-4002", Material.MaterialType.FINISHED)
    response = planner.post(f"{API}/boms/", {
        "material": "FG-4002", "plant": "1000", "valid_from": "2026-04-01", "description": "Gadget",
        "items": [{"component": "RM-2001", "quantity": "1.2"}, {"component": "RM-2002", "quantity": "2"}],
    }, format="json")
    assert response.status_code == 201, response.data
    assert response.data["key"] == "1000/FG-4002/2026-04-01"
    assert [(i["item_number"], i["component"], i["quantity"]) for i in response.data["items"]] == [
        (10, "RM-2001", "1.200"), (20, "RM-2002", "2.000")]
    assert services.find_bom(gadget, pp.plant, date(2026, 3, 31)) is None
    listed = planner.get(f"{API}/boms/?material=FG-4002&plant=1000&status=ACTIVE")
    assert listed.status_code == 200 and listed.data["count"] == 1


@pytest.mark.parametrize("material, items, message", [
    ("RM-2001", [("RM-2002", "1")], "only finished and semi-finished products"),
    ("FG-4001", [("FG-4001", "1")], "cannot be a component of itself"),
    ("FG-4001", [("RM-2001", "1"), ("RM-2001", "2")], "only once"),
    ("FG-4001", [], "at least one component"),
    ("FG-4001", [("RM-2001", "0")], "greater than zero"),
])
def test_bom_rules(pp, material, items, message):
    with pytest.raises(services.ProductionError, match=message):
        services.create_bom(material=pp.material(material), plant=pp.plant, valid_from=date(2026, 5, 1),
                            items=[services.ComponentInput(pp.material(n), D(q)) for n, q in items])


def test_bom_rejects_services_inactive_components_and_duplicate_versions(pp):
    fitting = new_material("SV-1", Material.MaterialType.SERVICE)
    bracket = pp.material("FG-4001")
    with pytest.raises(services.ProductionError, match="is a service"):
        services.create_bom(material=bracket, plant=pp.plant, valid_from=date(2026, 5, 1),
                            items=[services.ComponentInput(fitting, D("1"))])
    old = new_material("RM-OLD")
    old.is_active = False
    old.save()
    with pytest.raises(services.ProductionError, match="inactive"):
        services.create_bom(material=bracket, plant=pp.plant, valid_from=date(2026, 5, 1),
                            items=[services.ComponentInput(old, D("1"))])
    with pytest.raises(services.ProductionError, match="later date"):
        services.create_bom(material=bracket, plant=pp.plant, valid_from=START,
                            items=[services.ComponentInput(pp.material("RM-2001"), D("1"))])
    with pytest.raises(services.ProductionError, match="once"):
        services.create_bom(material=bracket, plant=pp.plant, valid_from=date(2026, 5, 1),
                            items=[services.ComponentInput(pp.material("RM-2001"), D("1"), 10),
                                   services.ComponentInput(pp.material("RM-2002"), D("1"), 10)])


def test_bom_cycles_are_refused_at_any_depth(pp, planner):
    # FG-4001 already uses SF-3001. Making SF-3001 out of FG-4001 would loop forever.
    response = planner.post(f"{API}/boms/", {
        "material": "SF-3001", "plant": "1000", "valid_from": "2026-06-01",
        "items": [{"component": "FG-4001", "quantity": "1"}],
    }, format="json")
    assert response.status_code == 400
    assert "SF-3001 -> FG-4001 -> SF-3001" in str(response.data)


def test_deactivate_and_reactivate_a_bom(pp, planner):
    bom = BillOfMaterials.objects.get(material__number="FG-4001")
    response = planner.post(f"{API}/boms/{bom.pk}/status/", {"status": "INACTIVE"}, format="json")
    assert response.status_code == 200 and response.data["status"] == "INACTIVE"
    assert services.find_bom(pp.material("FG-4001"), pp.plant, date(2026, 6, 1)) is None

    # While it is inactive, a BOM that would close a loop is allowed; reactivating the first one is not.
    services.create_bom(material=pp.material("SF-3001"), plant=pp.plant, valid_from=date(2026, 6, 1),
                        items=[services.ComponentInput(pp.material("FG-4001"), D("1"))])
    response = planner.post(f"{API}/boms/{bom.pk}/status/", {"status": "ACTIVE"}, format="json")
    assert response.status_code == 400 and "component of itself" in str(response.data)


def test_multi_level_explosion(pp, planner):
    response = planner.get(f"{API}/boms/explode/?material=FG-4001&plant=1000&quantity=10&date=2026-06-01")
    assert response.status_code == 200, response.data
    rows = [(r["level"], r["path"], r["quantity"], r["has_bom"]) for r in response.data["components"]]
    assert rows == [
        (1, "FG-4001/SF-3001", D("10.000"), True),
        (2, "FG-4001/SF-3001/RM-2001", D("25.000"), False),
        (1, "FG-4001/RM-2002", D("40.000"), False),
    ]
    assert planner.get(f"{API}/boms/explode/?material=RM-2001&plant=1000").status_code == 400
    bad = planner.get(f"{API}/boms/explode/?material=NOPE&plant=XX&quantity=-1&date=June")
    assert set(bad.data) == {"material", "plant", "quantity", "date"}


def test_the_version_valid_on_the_date_is_used(pp):
    bracket = pp.material("FG-4001")
    services.create_bom(material=bracket, plant=pp.plant, valid_from=date(2026, 9, 1), base_quantity=D("2"),
                        items=[services.ComponentInput(pp.material("SF-3001"), D("2")),
                               services.ComponentInput(pp.material("RM-2002"), D("6"))])
    before = services.explode_bom(bracket, pp.plant, D("10"), date(2026, 8, 31))
    after = services.explode_bom(bracket, pp.plant, D("10"), date(2026, 9, 1))
    assert [r["quantity"] for r in before if r["component"] == "RM-2002"] == [D("40.000")]
    assert [r["quantity"] for r in after if r["component"] == "RM-2002"] == [D("30.000")]  # 6 per 2


# --- routings --------------------------------------------------------------------------------------

def test_create_routing_through_the_api(pp, planner):
    new_material("FG-4002", Material.MaterialType.FINISHED)
    response = planner.post(f"{API}/routings/", {
        "material": "FG-4002", "plant": "1000", "valid_from": "2026-04-01",
        "operations": [
            {"description": "Press", "work_center": "PRESS3",
             "activities": [{"activity_type": "MACH", "setup_quantity": "0.25", "quantity_per_base": "0.05"}]},
            {"description": "Assemble", "work_center": "ASSY1",
             "activities": [{"activity_type": "LAB", "quantity_per_base": "0.2"}]},
        ],
    }, format="json")
    assert response.status_code == 201, response.data
    assert [(o["operation_number"], o["work_center"]) for o in response.data["operations"]] == [
        (10, "PRESS3"), (20, "ASSY1")]
    unknown = planner.post(f"{API}/routings/", {
        "material": "FG-4002", "plant": "1000", "valid_from": "2026-05-01",
        "operations": [{"description": "X", "work_center": "NOPE"}]}, format="json")
    assert "not found in plant 1000" in str(unknown.data)
    unknown_activity = planner.post(f"{API}/routings/", {
        "material": "FG-4002", "plant": "1000", "valid_from": "2026-05-01",
        "operations": [{"description": "X", "work_center": "ASSY1", "activities": [{"activity_type": "ZZ"}]}]},
        format="json")
    assert "Activity type ZZ not found" in str(unknown_activity.data)


@pytest.mark.parametrize("work_center, activities, message", [
    ("ASSY1", [("MACH", "0", "1")], "not recorded at work centre ASSY1"),
    ("PRESS3", [("MACH", "0", "0")], "has no quantity"),
    ("PRESS3", [("MACH", "0", "1"), ("MACH", "1", "0")], "appears twice"),
    ("PRESS3", [("MACH", "-1", "1")], "cannot be negative"),
])
def test_routing_rules(pp, work_center, activities, message):
    operation = services.OperationInput("Step", pp.wc(work_center), [
        services.ActivityInput(pp.activity(code), D(setup), D(per)) for code, setup, per in activities])
    with pytest.raises(services.ProductionError, match=message):
        services.create_routing(material=pp.material("FG-4001"), plant=pp.plant, valid_from=date(2026, 5, 1),
                                operations=[operation])


def test_routing_needs_active_work_centers_in_its_plant(pp):
    press = pp.wc("PRESS3")
    press.is_active = False
    press.save()
    step = services.OperationInput("Press", press)
    with pytest.raises(services.ProductionError, match="inactive"):
        services.create_routing(material=pp.material("FG-4001"), plant=pp.plant, valid_from=date(2026, 5, 1),
                                operations=[step])
    other = Plant.objects.create(code="2000", name="Durban", company_code=pp.company)
    with pytest.raises(services.ProductionError, match="not in plant 2000"):
        services.create_routing(material=pp.material("FG-4001"), plant=other, valid_from=date(2026, 5, 1),
                                operations=[step])
    with pytest.raises(services.ProductionError, match="at least one operation"):
        services.create_routing(material=pp.material("FG-4001"), plant=pp.plant, valid_from=date(2026, 5, 1),
                                operations=[])
    with pytest.raises(services.ProductionError, match="later date"):
        services.create_routing(material=pp.material("FG-4001"), plant=pp.plant, valid_from=START,
                                operations=[services.OperationInput("Assemble", pp.wc("ASSY1"))])


def test_deactivate_a_routing(pp, planner):
    routing = pp.material("FG-4001")
    record = services.find_routing(routing, pp.plant, date(2026, 6, 1))
    response = planner.post(f"{API}/routings/{record.pk}/status/", {"status": "INACTIVE"}, format="json")
    assert response.status_code == 200 and response.data["status"] == "INACTIVE"
    assert services.find_routing(routing, pp.plant, date(2026, 6, 1)) is None


# --- standard cost estimate ------------------------------------------------------------------------

def test_cost_estimate_matches_the_hand_calculation(pp, client_for):
    response = client_for(Role.FINANCE_MANAGER).get(
        f"{API}/cost-estimate/?material=FG-4001&plant=1000&quantity=100&date=2026-06-01")
    assert response.status_code == 200, response.data
    estimate = response.data
    assert (estimate["total"], estimate["unit_cost"], estimate["currency"]) == (D("22525.00"), D("225.25"), "ZAR")
    frame, fasteners = estimate["materials"]
    assert (frame["component"], frame["source"], frame["value"]) == ("SF-3001", "rolled_up", D("16425.00"))
    assert frame["estimate"]["material_cost"] == D("9500.00")
    assert [(a["activity_type"], a["quantity"], a["value"]) for a in frame["estimate"]["activities"]] == [
        ("LAB", D("10.000"), D("2200.00")), ("MACH", D("10.500"), D("4725.00"))]
    assert (fasteners["source"], fasteners["price"], fasteners["value"]) == ("moving_average", D("1.5000"),
                                                                             D("600.00"))
    assert [(a["work_center"], a["value"]) for a in estimate["activities"]] == [("ASSY1", D("5500.00"))]


def test_cost_estimate_failures(pp):
    widget = new_material("FG-9")
    widget.material_type = Material.MaterialType.FINISHED
    widget.save()
    nothing = new_material("RM-NOPRICE")
    services.create_bom(material=widget, plant=pp.plant, valid_from=START,
                        items=[services.ComponentInput(nothing, D("1"))])
    with pytest.raises(services.ProductionError, match="No price for RM-NOPRICE"):
        services.cost_estimate(widget, pp.plant, D("1"), START)
    with pytest.raises(services.ProductionError, match="No active BOM"):
        services.cost_estimate(pp.material("RM-2001"), pp.plant, D("1"), START)
    with pytest.raises(services.ProductionError, match="greater than zero"):
        services.cost_estimate(pp.material("FG-4001"), pp.plant, D("0"), START)
    # A routing activity without a rate cannot be costed.
    pp.activity("LAB").rates.all().delete()
    with pytest.raises(services.ProductionError, match="No rate for activity LAB"):
        services.cost_estimate(pp.material("FG-4001"), pp.plant, D("1"), START)


def test_cost_estimate_api_errors(pp, client_for):
    finance = client_for(Role.FINANCE_MANAGER)
    response = finance.get(f"{API}/cost-estimate/?material=RM-2001&plant=1000")
    assert response.status_code == 400 and "No active BOM" in str(response.data)


# --- knowledge graph -------------------------------------------------------------------------------

def test_projection_of_equipment_boms_and_routings(pp, settings):
    settings.GRAPH_ENABLED = True
    store = MemoryStore()
    processor.rebuild(store=store)
    cell = store.props("Cell", "1000/PRESS3")
    assert (cell["isa95_level"], cell["uns_path"], cell["activity_types"]) == ("WorkCell", "1000/1000/assy/l1/press3",
                                                                               ["LAB", "MACH"])
    assert store.related("Cell", "1000/PRESS3", "PART_OF") == [("Line", "1000/L1", {})]
    assert store.related("Cell", "1000/PRESS3", "CHARGED_TO") == [("CostCenter", "1000/4000", {})]
    assert store.related("Line", "1000/L1", "PART_OF") == [("Area", "1000/ASSY", {})]
    assert store.related("Area", "1000/ASSY", "PART_OF") == [("Plant", "1000", {})]
    assert store.props("Line", "1000/L1")["isa95_type"] == "ProductionLine"

    bom = "1000/FG-4001/2026-03-01"
    assert store.related("BOM", bom, "FOR") == [("Material", "FG-4001", {})]
    consumes = sorted((to, props["quantity"]) for _, to, props in store.related("BOM", bom, "CONSUMES"))
    assert consumes == [("RM-2002", 4.0), ("SF-3001", 1.0)]
    operations = store.related("Routing", "1000/SF-3001/2026-03-01", "HAS_OPERATION")
    assert operations == [("Cell", "1000/PRESS3", {"operation_number": 10, "description": "Press frame"})]


def test_bom_changes_flow_through_the_outbox(pp, settings):
    settings.GRAPH_ENABLED = True
    OutboxEvent.objects.all().delete()
    bom = BillOfMaterials.objects.get(material__number="FG-4001")
    services.set_bom_status(bom, BillOfMaterials.Status.INACTIVE)
    store = MemoryStore()
    assert processor.process_outbox(store=store)["failed"] == 0
    assert store.props("BOM", bom.key)["status"] == "INACTIVE"


def test_everyone_sees_production_master_data_in_the_graph(make_user):
    for role in (Role.PRODUCTION_PLANNER, Role.SALES_REP, Role.HR_ADMIN):
        labels = access.allowed_labels(make_user(role, username=f"pp-{role}"))
        assert {"Area", "Line", "Cell", "BOM", "Routing"} <= labels
    planner = access.allowed_labels(make_user(Role.PRODUCTION_PLANNER, username="pp-planner-2"))
    assert "SalesOrder" in planner and "Employee" not in planner and "Invoice" not in planner


# --- logging ---------------------------------------------------------------------------------------

def test_django_does_not_repeat_4xx_warnings():
    # The API logs each rejected request once, with its request ID; Django's own WARNING would repeat it.
    assert not logging.getLogger("django.request").isEnabledFor(logging.WARNING)
    assert logging.getLogger("django.request").isEnabledFor(logging.ERROR)
