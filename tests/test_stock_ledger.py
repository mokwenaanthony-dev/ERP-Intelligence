"""Stock-movement ledger: movements, moving-average valuation, journal integration and reversal.

The demo company starts with opening stock in plant 1000, storage location 0001:
100 x TG-1001 at R120 (R12,000) and 50 x TG-1002 at R300 (R15,000).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.finance import services as finance
from apps.finance.models import GLAccount, ImmutableDocumentError, JournalEntry, JournalLine, PostingPeriod
from apps.inventory import services
from apps.inventory.models import AccountKey, InventoryAccountSetting, MaterialDocument, MovementType
from apps.inventory.services import ItemInput, StockError
from apps.masterdata.models import Material, MaterialPlant, UnitOfMeasure
from apps.organization.models import Plant, StorageLocation

pytestmark = pytest.mark.django_db

MAY_15 = date(2026, 5, 15)


@pytest.fixture
def stock(demo):
    plant = Plant.objects.get(code="1000")
    return SimpleNamespace(
        demo=demo, plant=plant,
        main=StorageLocation.objects.get(plant=plant, code="0001"),
        dispatch=StorageLocation.objects.get(plant=plant, code="0002"),
        widget=Material.objects.get(number="TG-1001"),
        gadget=Material.objects.get(number="TG-1002"),
    )


def move(stock, movement_type, *items, **kwargs):
    kwargs.setdefault("posting_date", MAY_15)
    return services.post_goods_movement(movement_type=movement_type, plant=stock.plant, items=list(items), **kwargs)


def item(stock, material, quantity, location=None, **kwargs):
    return ItemInput(material=material, storage_location=location or stock.main, quantity=Decimal(str(quantity)),
                     **kwargs)


def journal(document):
    return [(line.gl_account.number, line.amount) for line in document.journal_entry.lines.order_by("line_number")]


def plant_stock(stock, material):
    return services.plant_stock(material, stock.plant)


# --- Opening stock (posted by the demo command) -------------------------------------------

def test_opening_stock_is_valued_and_posted(stock):
    assert plant_stock(stock, stock.widget) == (Decimal("100"), Decimal("12000.00"))
    assert plant_stock(stock, stock.gadget) == (Decimal("50"), Decimal("15000.00"))
    document = MaterialDocument.objects.get(document_number="MD00000001")
    assert (document.movement_type, document.journal_entry.document_number) == ("561", "WE00000001")
    assert journal(document) == [
        ("1200", Decimal("12000.00")), ("3200", Decimal("-12000.00")),
        ("1200", Decimal("15000.00")), ("3200", Decimal("-15000.00")),
    ]
    line = document.journal_entry.lines.first()
    assert (line.material, line.plant) == (stock.widget, stock.plant)


# --- Receipts and issues ----------------------------------------------------------------

def test_goods_receipt_updates_moving_average_and_posts_gr_ir(stock, make_user):
    user = make_user(username="storeman")
    document = move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 50, unit_price=Decimal("150")),
                    business_partner=stock.demo.supplier, reference="DN-778", user=user)

    assert (document.document_number, document.journal_entry.document_number) == ("MD00000002", "WE00000002")
    assert (document.business_partner, document.created_by) == (stock.demo.supplier, user)
    quantity, value = plant_stock(stock, stock.widget)
    assert (quantity, value) == (Decimal("150"), Decimal("19500.00"))
    assert services.moving_average_price(quantity, value) == Decimal("130.0000")
    assert journal(document) == [("1200", Decimal("7500.00")), ("2300", Decimal("-7500.00"))]
    gr_ir_line = document.journal_entry.lines.get(gl_account__number="2300")
    assert gr_ir_line.business_partner == stock.demo.supplier


def test_goods_issue_is_valued_at_moving_average(stock):
    move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 50, unit_price=Decimal("150")))
    document = move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 30))
    assert document.journal_entry.document_number == "WA00000001"
    assert journal(document) == [("1200", Decimal("-3900.00")), ("5000", Decimal("3900.00"))]  # 30 x R130
    assert plant_stock(stock, stock.widget) == (Decimal("120"), Decimal("15600.00"))


def test_issuing_all_stock_takes_the_full_remaining_value(stock):
    move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 1, unit_price=Decimal("0.01")))
    first = move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 1))
    assert first.items.get().amount_local == Decimal("-118.81")  # 12000.01 / 101, rounded
    move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 100))
    assert plant_stock(stock, stock.widget) == (Decimal("0"), Decimal("0.00"))  # no value left behind


def test_several_items_for_one_material_are_valued_in_sequence(stock):
    document = move(stock, MovementType.GOODS_ISSUE_DELIVERY,
                    item(stock, stock.widget, 50), item(stock, stock.widget, 50))
    assert [i.amount_local for i in document.items.all()] == [Decimal("-6000.00"), Decimal("-6000.00")]
    assert plant_stock(stock, stock.widget) == (Decimal("0"), Decimal("0.00"))


def test_insufficient_stock_posts_nothing(stock):
    documents, entries = MaterialDocument.objects.count(), JournalEntry.objects.count()
    with pytest.raises(StockError, match="Insufficient stock of TG-1001"):
        move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.gadget, 1), item(stock, stock.widget, 101))
    assert (MaterialDocument.objects.count(), JournalEntry.objects.count()) == (documents, entries)


def test_stock_is_checked_per_storage_location(stock):
    with pytest.raises(StockError, match="Insufficient stock"):
        move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 1, location=stock.dispatch))


@pytest.mark.parametrize(
    "movement_type, account, amount",
    [(MovementType.SCRAPPING, "5100", Decimal("600.00")), (MovementType.INVENTORY_LOSS, "5200", Decimal("600.00"))],
)
def test_scrapping_and_count_losses(stock, movement_type, account, amount):
    document = move(stock, movement_type, item(stock, stock.widget, 5))
    assert journal(document) == [("1200", -amount), (account, amount)]
    assert document.journal_entry.document_type == ("WA" if movement_type == MovementType.SCRAPPING else "WI")


def test_count_gain_without_price_uses_moving_average(stock):
    document = move(stock, MovementType.INVENTORY_GAIN, item(stock, stock.gadget, 1))
    assert journal(document) == [("1200", Decimal("300.00")), ("5200", Decimal("-300.00"))]


def test_count_gain_for_material_without_stock_needs_a_price(stock):
    new = Material.objects.create(number="TG-1003", description="New", base_unit=UnitOfMeasure.objects.get(code="EA"))
    with pytest.raises(StockError, match="give a unit price"):
        move(stock, MovementType.INVENTORY_GAIN, item(stock, new, 2))
    document = move(stock, MovementType.INVENTORY_GAIN, item(stock, new, 2, unit_price=Decimal("10")))
    assert document.items.get().amount_local == Decimal("20.00")


# --- Transfers --------------------------------------------------------------------------

def test_transfer_moves_quantity_without_value_or_journal(stock):
    document = move(stock, MovementType.TRANSFER, item(stock, stock.widget, 40, to_storage_location=stock.dispatch))
    assert document.journal_entry is None
    assert [(i.storage_location.code, i.quantity, i.amount_local) for i in document.items.all()] == [
        ("0001", Decimal("-40"), Decimal("0")), ("0002", Decimal("40"), Decimal("0")),
    ]
    assert services.location_stock(stock.widget, stock.main) == Decimal("60")
    assert services.location_stock(stock.widget, stock.dispatch) == Decimal("40")
    assert plant_stock(stock, stock.widget) == (Decimal("100"), Decimal("12000.00"))
    issue = move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 40, location=stock.dispatch))
    assert issue.items.get().amount_local == Decimal("-4800.00")


def test_transfer_rules(stock):
    with pytest.raises(StockError, match="destination"):
        move(stock, MovementType.TRANSFER, item(stock, stock.widget, 1))
    with pytest.raises(StockError, match="must differ"):
        move(stock, MovementType.TRANSFER, item(stock, stock.widget, 1, to_storage_location=stock.main))
    other_plant = Plant.objects.create(code="2000", name="Durban", company_code=stock.demo.company)
    elsewhere = StorageLocation.objects.create(plant=other_plant, code="0001", name="Durban store")
    with pytest.raises(StockError, match="not in plant 1000"):
        move(stock, MovementType.TRANSFER, item(stock, stock.widget, 1, to_storage_location=elsewhere))
    with pytest.raises(StockError, match="Insufficient stock"):
        move(stock, MovementType.TRANSFER, item(stock, stock.widget, 101, to_storage_location=stock.dispatch))


# --- Validation ---------------------------------------------------------------------------

def test_input_rules(stock):
    service = Material.objects.create(number="SV-1", description="Installation", material_type="DIEN",
                                      base_unit=UnitOfMeasure.objects.get(code="HR"))
    inactive = Material.objects.create(number="OLD-1", description="Old", is_active=False,
                                       base_unit=UnitOfMeasure.objects.get(code="EA"))
    other_plant = Plant.objects.create(code="2000", name="Durban", company_code=stock.demo.company)
    elsewhere = StorageLocation.objects.create(plant=other_plant, code="0001", name="Durban store")
    cases = [
        ("999", [item(stock, stock.widget, 1)], "Unknown movement type"),
        (MovementType.GOODS_RECEIPT, [], "at least one item"),
        (MovementType.GOODS_RECEIPT, [item(stock, stock.widget, 0, unit_price=Decimal("1"))], "greater than zero"),
        (MovementType.GOODS_RECEIPT, [item(stock, stock.widget, 1)], "needs a unit price"),
        (MovementType.GOODS_RECEIPT, [item(stock, stock.widget, 1, unit_price=Decimal("-1"))], "needs a unit price"),
        (MovementType.GOODS_RECEIPT, [item(stock, service, 1, unit_price=Decimal("1"))], "is a service"),
        (MovementType.GOODS_RECEIPT, [item(stock, inactive, 1, unit_price=Decimal("1"))], "not active"),
        (MovementType.GOODS_RECEIPT, [item(stock, stock.widget, 1, location=elsewhere, unit_price=Decimal("1"))],
         "not in plant 1000"),
    ]
    for movement_type, items, message in cases:
        with pytest.raises(StockError, match=message):
            move(stock, movement_type, *items)


def test_closed_period_blocks_movements(stock):
    finance.set_period_status(finance.find_period(stock.demo.company, MAY_15), PostingPeriod.Status.CLOSED)
    with pytest.raises(StockError, match="is closed"):
        move(stock, MovementType.TRANSFER, item(stock, stock.widget, 1, to_storage_location=stock.dispatch))


def test_missing_account_determination_is_reported(stock):
    InventoryAccountSetting.objects.filter(account_key=AccountKey.COST_OF_SALES).delete()
    with pytest.raises(StockError, match="No COST_OF_SALES account"):
        move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 1))


def test_material_type_specific_account_overrides_default(stock):
    finished_goods = GLAccount.objects.create(chart_of_accounts=stock.demo.chart, number="1210",
                                              name="Inventory - finished goods", account_type="ASSET")
    InventoryAccountSetting.objects.create(chart_of_accounts=stock.demo.chart, account_key=AccountKey.INVENTORY,
                                           material_type="FERT", gl_account=finished_goods)
    product = Material.objects.create(number="FG-1", description="Assembled unit", material_type="FERT",
                                      base_unit=UnitOfMeasure.objects.get(code="EA"))
    document = move(stock, MovementType.INITIAL_ENTRY, item(stock, product, 2, unit_price=Decimal("500")))
    assert journal(document)[0] == ("1210", Decimal("1000.00"))
    assert MaterialPlant.objects.filter(material=product, plant=stock.plant).exists()


# --- Reversal ---------------------------------------------------------------------------

def test_reversing_a_receipt_restores_stock_and_reverses_the_journal(stock, make_user):
    receipt = move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 50, unit_price=Decimal("150")))
    user = make_user(username="controller")
    reversal = services.reverse_material_document(receipt, user=user)

    receipt.refresh_from_db()
    assert (receipt.status, reversal.reversal_of, reversal.created_by) == ("REVERSED", receipt, user)
    assert reversal.header_text == f"Reversal of {receipt.document_number}"
    assert plant_stock(stock, stock.widget) == (Decimal("100"), Decimal("12000.00"))
    receipt.journal_entry.refresh_from_db()
    assert receipt.journal_entry.status == "REVERSED"
    assert reversal.journal_entry.reversal_of == receipt.journal_entry

    with pytest.raises(StockError, match="already been reversed"):
        services.reverse_material_document(receipt)
    with pytest.raises(StockError, match="itself a reversal"):
        services.reverse_material_document(reversal)


def test_receipt_cannot_be_reversed_once_its_stock_is_issued(stock):
    receipt = move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 50, unit_price=Decimal("150")))
    move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 120))
    with pytest.raises(StockError, match="already been issued"):
        services.reverse_material_document(receipt)
    receipt.refresh_from_db()
    assert receipt.status == "POSTED"


def test_reversal_may_not_leave_value_without_quantity(stock):
    move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 100, unit_price=Decimal("180")))
    move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 100))  # 100 left, worth R15,000
    opening = MaterialDocument.objects.get(document_number="MD00000001")
    with pytest.raises(StockError, match="would leave TG-1001"):
        services.reverse_material_document(opening)


def test_reversing_an_issue_and_a_transfer(stock):
    issue = move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 10))
    services.reverse_material_document(issue)
    assert plant_stock(stock, stock.widget) == (Decimal("100"), Decimal("12000.00"))

    transfer = move(stock, MovementType.TRANSFER, item(stock, stock.widget, 10, to_storage_location=stock.dispatch))
    reversal = services.reverse_material_document(transfer)
    assert reversal.journal_entry is None
    assert services.location_stock(stock.widget, stock.dispatch) == 0


def test_reversal_into_closed_period_is_rejected(stock):
    issue = move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 10))
    finance.set_period_status(finance.find_period(stock.demo.company, MAY_15), PostingPeriod.Status.CLOSED)
    with pytest.raises(StockError, match="is closed"):
        services.reverse_material_document(issue)


# --- Reports ------------------------------------------------------------------------------

def test_stock_overview(stock):
    move(stock, MovementType.TRANSFER, item(stock, stock.widget, 40, to_storage_location=stock.dispatch))
    rows = {row["material"]: row for row in services.stock_overview(plant=stock.plant)}
    widget = rows["TG-1001"]
    assert (widget["quantity"], widget["value"], widget["moving_average_price"]) == (
        Decimal("100"), Decimal("12000.00"), Decimal("120.0000"),
    )
    assert widget["storage_locations"] == {"0001": Decimal("60"), "0002": Decimal("40")}
    assert widget["unit"] == "EA"
    assert [row["material"] for row in services.stock_overview(material=stock.gadget)] == ["TG-1002"]


def test_zero_quantity_locations_are_left_out_of_the_overview(stock):
    move(stock, MovementType.TRANSFER, item(stock, stock.gadget, 50, to_storage_location=stock.dispatch))
    row = services.stock_overview(material=stock.gadget)[0]
    assert row["storage_locations"] == {"0002": Decimal("50")}


def test_stock_value_reconciles_with_the_ledger(stock):
    move(stock, MovementType.GOODS_RECEIPT, item(stock, stock.widget, 7, unit_price=Decimal("133.33")))
    move(stock, MovementType.GOODS_ISSUE_DELIVERY, item(stock, stock.widget, 33), item(stock, stock.gadget, 3))
    move(stock, MovementType.SCRAPPING, item(stock, stock.gadget, 1))
    report = services.stock_reconciliation(stock.demo.company)
    assert report["reconciled"] is True
    assert report["stock_value"] == report["ledger_value"]

    # A manual journal posting to the inventory account breaks the reconciliation, and the report shows it.
    finance.post_journal_entry(company_code=stock.demo.company, posting_date=MAY_15, lines=[
        finance.LineInput(stock.demo.account("1200"), "D", Decimal("10")),
        finance.LineInput(stock.demo.account("1000"), "C", Decimal("10")),
    ])
    report = services.stock_reconciliation(stock.demo.company)
    assert (report["reconciled"], report["difference"]) == (False, Decimal("-10.00"))


def test_helpers():
    assert services.moving_average_price(Decimal("0"), Decimal("0")) is None
    assert services.issue_value(Decimal("1"), Decimal("3"), Decimal("10.00")) == Decimal("3.33")
    assert services.issue_value(Decimal("3"), Decimal("3"), Decimal("10.00")) == Decimal("10.00")


# --- Immutability ---------------------------------------------------------------------------

def test_material_documents_are_immutable(stock):
    document = MaterialDocument.objects.get(document_number="MD00000001")
    item_row = document.items.first()
    for action in (document.save, document.delete, item_row.save, item_row.delete):
        with pytest.raises(ImmutableDocumentError):
            action()


def test_string_representations(stock):
    document = MaterialDocument.objects.get(document_number="MD00000001")
    assert str(document) == "1000/2027/MD00000001 (561)"
    assert str(document.items.first()) == "1000/2027/MD00000001 (561) item 1: TG-1001 100.000"
    assert str(MaterialPlant.objects.get(material=stock.widget)) == "TG-1001 @ 1000"
    setting = InventoryAccountSetting.objects.get(account_key=AccountKey.INVENTORY)
    assert str(setting) == "CAZA INVENTORY (all) -> 1200"
    assert JournalLine.objects.filter(material=stock.widget).count() == 2
