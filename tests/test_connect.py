"""Wave 10b: side by side. Another ERP's exports load through our rules, idempotently, row by row.

The files are the samples in ``docs/import-samples/`` (an invented company's export in the generic
layout of ``docs/IMPORT_FORMAT.md``), so the documented samples cannot drift from what loads. Each
holds one row that must be rejected, with its reason, while the others load.
"""
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Role
from apps.connect import services
from apps.connect.mappings import MAPPINGS, FileError, Mapping
from apps.connect.models import ExternalReference, ImportBatch
from apps.inventory import services as inventory
from apps.masterdata.models import BusinessPartner, CustomerRole, Material, SupplierRole
from apps.organization.models import Plant, StorageLocation
from apps.purchasing.models import PurchaseOrder
from apps.sales.models import SalesOrder

pytestmark = pytest.mark.django_db
D = Decimal
ON = date(2026, 11, 15)
SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "import-samples"
API = "/api/connect"


def sample(name: str) -> bytes:
    return (SAMPLES / f"{name}.csv").read_bytes()


def load(demo, user, object_type, content=None):
    """Load one sample (or the given content) into the demo company as the generic source."""
    return services.import_file(demo.company, source="generic", object_type=object_type,
                                content=content if content is not None else sample(object_type), user=user, on=ON)


def by_key(batch):
    return {r["key"]: r for r in batch.results}


@pytest.fixture
def loader(demo, make_user):
    return make_user(Role.FINANCE_MANAGER)


@pytest.fixture
def loaded(demo, loader):
    """Every sample loaded, in the documented order."""
    batches = {name: load(demo, loader, name)[0]
               for name in ("partners", "materials", "stock", "purchase_orders", "sales_orders")}
    demo.batches = batches
    return demo


# --- partners and materials ------------------------------------------------------------------------------------

def test_partners_load_with_roles_and_link_by_vat_number(demo, loader):
    batch, created = load(demo, loader, "partners")
    assert created and (batch.rows, batch.created, batch.updated, batch.rejected) == (6, 4, 1, 1)
    rows = by_key(batch)
    assert rows["S-0201"]["status"] == "updated" and rows["S-0201"]["number"] == demo.supplier.number
    assert "linked to existing partner" in rows["S-0201"]["detail"]  # Highveld, same VAT number: no duplicate
    assert rows["X-0401"]["status"] == "rejected" and "role must be CUSTOMER, SUPPLIER or BOTH" in \
        rows["X-0401"]["detail"]
    both = BusinessPartner.objects.get(number=rows["B-0301"]["number"])
    assert CustomerRole.objects.filter(business_partner=both).exists() and \
        SupplierRole.objects.filter(business_partner=both).exists()


def test_the_same_file_again_changes_nothing_and_a_changed_row_is_updated(demo, loader):
    first, _ = load(demo, loader, "partners")
    again, created = load(demo, loader, "partners")
    assert not created and again.pk == first.pk and ImportBatch.objects.count() == 1
    changed = sample("partners").replace(b"Durban Hardware Hub CC", b"Durban Hardware Hub (Pty) Ltd")
    second, created = load(demo, loader, "partners", changed)
    assert created and by_key(second)["C-0102"]["status"] == "updated"
    assert sum(r["status"] == "unchanged" for r in second.results) == 4  # the others, and X-0401 is still rejected
    assert BusinessPartner.objects.get(number=by_key(second)["C-0102"]["number"]).name == \
        "Durban Hardware Hub (Pty) Ltd"
    assert BusinessPartner.objects.filter(name__startswith="Durban").count() == 1


def test_materials_load_and_an_unknown_unit_is_named(demo, loader):
    batch, _ = load(demo, loader, "materials")
    rows = by_key(batch)
    assert (batch.created, batch.updated, batch.rejected) == (3, 1, 1)
    assert Material.objects.get(number="ITM-502").material_type == "ROH"
    assert rows["TG-1001"]["detail"] == "linked to existing material TG-1001"
    assert rows["ITM-599"]["detail"].startswith("Unit 'CRATE' is not known: map it to one of BOX, EA")


# --- stock: a snapshot, posted as differences ------------------------------------------------------------------

def test_stock_is_a_snapshot_posted_as_differences(demo, loader):
    load(demo, loader, "partners")
    load(demo, loader, "materials")
    batch, _ = load(demo, loader, "stock")
    rows = by_key(batch)
    assert (batch.updated, batch.rejected) == (3, 1)
    assert "(movement 561)" in rows["ITM-500@1000"]["detail"]  # the first stock: an initial entry
    assert "No material 'ITM-777' has been loaded from generic" in rows["ITM-777@1000"]["detail"]
    main = StorageLocation.objects.filter(plant__code="1000").order_by("code").first()
    bolt = Material.objects.get(number="ITM-500")
    assert inventory.location_stock(bolt, main) == D("2000.000")
    counted = sample("stock").replace(b"ITM-500,1000,2000,", b"ITM-500,1000,1800,")
    recount, _ = load(demo, loader, "stock", counted)
    assert "2000.000 -> 1800 at 1000/" in by_key(recount)["ITM-500@1000"]["detail"]
    assert "(movement 702)" in by_key(recount)["ITM-500@1000"]["detail"]  # a count loss of 200
    assert by_key(recount)["ITM-501@1000"]["status"] == "unchanged"
    assert inventory.location_stock(bolt, main) == D("1800.000")
    assert inventory.stock_reconciliation(demo.company)["reconciled"] is True  # stock value = inventory account


# --- open orders --------------------------------------------------------------------------------------------------

def test_open_orders_load_through_the_order_rules(loaded):
    po_rows, so_rows = by_key(loaded.batches["purchase_orders"]), by_key(loaded.batches["sales_orders"])
    assert po_rows["PO-X3-9001"]["status"] == "created" and po_rows["PO-X3-9001"]["detail"] == "2 line(s)"
    order = PurchaseOrder.objects.get(order_number=po_rows["PO-X3-9001"]["number"])
    assert order.status == PurchaseOrder.Status.RELEASED and order.supplier_reference == "PO-X3-9001"
    assert "No partner 'S-9999' has been loaded" in po_rows["PO-X3-9003"]["detail"]
    sale = SalesOrder.objects.get(order_number=so_rows["SO-X3-7001"]["number"])
    assert sale.customer_reference == "SO-X3-7001" and sale.items.count() == 2
    assert sale.items.get(item_number=10).net_price == D("7.90")  # the source's price, not our price list
    assert "is not a customer in 1000" in so_rows["SO-X3-7003"]["detail"]  # Vaal Fasteners is a supplier only
    assert so_rows["SO-X3-7002"]["status"] == "created"  # TG-1001, linked to our existing material


def test_an_order_loaded_once_is_never_created_twice(loaded, loader):
    again, created = load(loaded, loader, "purchase_orders", sample("purchase_orders") + b"\n")
    assert created and by_key(again)["PO-X3-9001"]["status"] == "unchanged"  # a new file, the same orders
    line = b"PO-X3-9001,20,S-0202,ITM-501,"
    changed = sample("purchase_orders").replace(line + b"2000,", line + b"2500,")
    refused, _ = load(loaded, loader, "purchase_orders", changed)
    assert "changed in generic after it was loaded" in by_key(refused)["PO-X3-9001"]["detail"]
    assert PurchaseOrder.objects.filter(supplier_reference="PO-X3-9001").count() == 1


def test_imported_data_reaches_planning_and_the_portal(loaded):
    """Side by side means the intelligence works on the other ERP's data: MRP, the control tower, the portal."""
    from apps.planning import mrp
    from apps.portal import services as portal
    from apps.portal.models import PartnerAccess

    receipts = mrp.snapshot(Plant.objects.get(code="1000"), ON, 6)["materials"]["ITM-500"]["receipts"]
    assert ["2026-11-28", "1000.000"] in receipts  # Vaal's open order, from the other ERP
    user = loaded.batches["partners"].imported_by.__class__.objects.create_user("hv", password="x", role=Role.SUPPLIER)
    access = PartnerAccess.objects.create(user=user, company_code=loaded.company, partner=loaded.supplier,
                                          kind="SUPPLIER")
    seen = [o["order_number"] for o in portal.purchase_orders(access)]
    assert by_key(loaded.batches["purchase_orders"])["PO-X3-9002"]["number"] in seen  # Highveld sees it


# --- files and mappings -------------------------------------------------------------------------------------------

def test_files_that_cannot_be_read_load_nothing(demo, loader):
    with pytest.raises(FileError, match="Missing column"):
        load(demo, loader, "partners", b"external_id,name\nC1,Someone\n")
    with pytest.raises(FileError, match="not UTF-8"):
        load(demo, loader, "partners", "external_id;name;role\n1;Caf\xe9;CUSTOMER\n".encode("latin-1"))
    with pytest.raises(FileError, match="Unknown source"):
        services.import_file(demo.company, source="oracle", object_type="partners", content=b"x", user=loader)
    assert ImportBatch.objects.count() == 0


def test_excel_style_files_load_too(demo, loader):
    """A byte-order mark, semicolons and spaces, as Excel writes CSV with South African settings."""
    content = ("﻿external_id ; name ; role\n C-9 ; Excel Trading ; customer \n\n").encode("utf-8")
    batch, _ = load(demo, loader, "partners", content)
    assert (batch.rows, batch.created) == (1, 1)


def test_a_mapping_translates_another_erps_columns_and_codes(demo, loader, monkeypatch):
    """The layer a Sage X3 or SAP mapping uses: their headings, their codes, their dates and decimals."""
    other = Mapping(name="other-erp", title="Another ERP (test)",
                    columns={"partners": {"external_id": "BPCNUM", "name": "BPCNAM", "role": "BPTYP"},
                             "materials": {"external_id": "ITMREF", "description": "ITMDES1", "unit": "STU"},
                             "stock": {"material": "ITMREF", "plant": "STOFCY", "quantity": "QTYSTU",
                                       "unit_price": "PRI"}},
                    values={"role": {"1": "CUSTOMER", "2": "SUPPLIER"}, "unit": {"UN": "EA"},
                            "plant": {"JHB": "1000"}},
                    date_formats=("%d.%m.%Y",), decimal_comma=True)
    monkeypatch.setitem(MAPPINGS, other.name, other)

    def go(object_type, content):
        """Load content through the test mapping."""
        return services.import_file(demo.company, source="other-erp", object_type=object_type, content=content,
                                    user=loader, on=ON)[0]

    assert go("partners", b"BPCNUM;BPCNAM;BPTYP\nK100;Kimberley Tools;1\n").created == 1
    assert go("materials", b"ITMREF;ITMDES1;STU\nBOLT-8;Bolt M8;UN\n").created == 1
    stock = go("stock", b"ITMREF;STOFCY;QTYSTU;PRI\nBOLT-8;JHB;1.250,5;2,10\n")
    assert stock.updated == 1 and "-> 1250.5 at 1000/" in stock.results[0]["detail"]
    assert ExternalReference.objects.filter(source="other-erp").count() == 2


# --- API and command --------------------------------------------------------------------------------------------

def test_the_import_api(demo, client_for):
    finance = client_for(Role.FINANCE_MANAGER)

    def upload(client, content=None):
        """POST the partners sample (or content) as a file."""
        file = SimpleUploadedFile("partners.csv", content or sample("partners"))
        return client.post(f"{API}/imports/", {"company_code": "1000", "source": "generic", "object": "partners",
                                               "file": file}, format="multipart")

    first = upload(finance)
    assert first.status_code == 201 and first.json()["created"] == 4 and len(first.json()["results"]) == 6
    assert upload(finance).status_code == 200  # the same file: the first batch
    assert upload(finance, b"nothing,useful\n").status_code == 400
    viewer = client_for(Role.VIEWER)
    assert upload(viewer).status_code == 403
    listed = viewer.get(f"{API}/imports/?company_code=1000").json()
    assert listed["count"] == 1 and "results" not in listed["imports"][0]
    detail = viewer.get(f"{API}/imports/{first.json()['id']}/").json()
    assert detail["results"][5]["status"] == "rejected"
    sources = viewer.get(f"{API}/sources/").json()
    assert sources["load_in_this_order"][0] == "partners" and sources["sources"][0]["name"] == "generic"


def test_the_import_command(demo, make_user):
    make_user(Role.FINANCE_MANAGER, username="finance")
    out = StringIO()
    file = str(SAMPLES / "partners.csv")
    call_command("import_data", "--company", "1000", "--object", "partners", "--file", file, "--user", "finance",
                 stdout=out)
    text = out.getvalue()
    assert "Loaded: partners.csv as partners from generic: 6 row(s), 4 created, 1 updated" in text
    assert "row 7 X-0401: role must be CUSTOMER, SUPPLIER or BOTH" in text
    call_command("import_data", "--company", "1000", "--object", "partners", "--file", file, "--user", "finance",
                 stdout=out)
    assert "Loaded before; retried 1 rejected row(s), 0 now loaded" in out.getvalue()  # X-0401 tried again
    with pytest.raises(CommandError, match="Missing column"):
        call_command("import_data", "--company", "1000", "--object", "stock", "--file", file, "--user", "finance",
                     stdout=out)


# --- v0.22.1: a rejected row gets another try when the same file comes again ------------------------------------

def test_orders_loaded_before_their_partners_load_when_the_file_comes_again(demo, loader):
    """Found in the v0.22.0 Docker demo: orders loaded before the partners stayed rejected for good."""
    early, _ = load(demo, loader, "purchase_orders")
    assert early.rejected == 3 and "load partners first" in by_key(early)["PO-X3-9001"]["detail"]
    load(demo, loader, "partners")
    load(demo, loader, "materials")
    again, created = load(demo, loader, "purchase_orders")
    assert not created and again.pk == early.pk and ImportBatch.objects.count() == 3  # the same batch
    assert (again.retried, again.now_loaded) == (3, 2)
    rows = by_key(again)
    assert rows["PO-X3-9001"]["status"] == "created" and rows["PO-X3-9002"]["status"] == "created"
    assert "No partner 'S-9999'" in rows["PO-X3-9003"]["detail"]  # its cause is still there
    assert (again.created, again.rejected) == (2, 1)
    third, _ = load(demo, loader, "purchase_orders")
    assert (third.retried, third.now_loaded) == (1, 0)  # only the one still rejected is tried
    assert PurchaseOrder.objects.filter(supplier_reference__startswith="PO-X3-").count() == 2  # never twice


def test_a_retry_never_reapplies_rows_that_loaded(demo, loader):
    """Stock that loaded is not posted again: a retry must not undo what happened since."""
    load(demo, loader, "partners")
    load(demo, loader, "materials")
    load(demo, loader, "stock")  # ITM-777 is rejected
    bolt, main = Material.objects.get(number="ITM-500"), StorageLocation.objects.get(plant__code="1000", code="0001")
    inventory.post_goods_movement(movement_type="702", plant=main.plant, posting_date=ON, user=loader,
                                  items=[inventory.ItemInput(material=bolt, storage_location=main, quantity=D("100"))])
    retry, _ = load(demo, loader, "stock")
    assert (retry.retried, retry.now_loaded) == (1, 0)
    assert inventory.location_stock(bolt, main) == D("1900.000")  # not brought back to the old 2000
