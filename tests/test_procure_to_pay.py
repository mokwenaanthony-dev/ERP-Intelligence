"""Procure-to-pay: purchase order -> goods receipt (101) -> supplier invoice (three-way match) -> payment.

Demo order used throughout (supplier Highveld Supplies, plant 1000, storage location 0001):
    item 10: 50 x TG-1001 @ R125.00 = R6,250.00
    item 20: 20 x TG-1002 @ R310.00 = R6,200.00          net R12,450.00, input VAT 15% R1,867.50
Opening stock is 100 x TG-1001 @ R120 and 50 x TG-1002 @ R300 (MD00000001 / WE00000001).
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.finance import services as finance
from apps.finance.models import JournalEntry
from apps.finance.services import PostingError
from apps.inventory import services as inventory
from apps.masterdata.models import Material
from apps.organization.models import Plant, PurchasingOrganization, StorageLocation
from apps.purchasing import services as purchasing
from apps.purchasing.models import GoodsReceipt, PurchaseOrder, PurchasingAccountSetting, SupplierInvoice
from apps.purchasing.services import InvoiceItemInput, OrderItemInput, PurchasingError, ReceiptItemInput

pytestmark = pytest.mark.django_db

ORDER_DAY, RECEIPT_DAY, INVOICE_DAY = date(2026, 5, 4), date(2026, 5, 11), date(2026, 5, 15)


def material(number):
    return Material.objects.get(number=number)


def make_order(demo, items=None, release=True, **kwargs):
    if items is None:
        items = [OrderItemInput(material("TG-1001"), Decimal("50"), Decimal("125.00")),
                 OrderItemInput(material("TG-1002"), Decimal("20"), Decimal("310.00"))]
    kwargs.setdefault("supplier", demo.supplier)
    order = purchasing.create_purchase_order(
        purchasing_organization=PurchasingOrganization.objects.get(code="1000"), order_date=ORDER_DAY,
        items=items, **kwargs,
    )
    return purchasing.release_order(order) if release else order


def receive(order, items=None, day=RECEIPT_DAY):
    return purchasing.post_goods_receipt(order, receipt_date=day, items=items)


def bill(order, reference="HS-9001", items=None, day=INVOICE_DAY):
    return purchasing.post_supplier_invoice(order, invoice_date=day, supplier_reference=reference, items=items)


def lines(entry):
    return [(line.gl_account.number, line.amount) for line in entry.lines.order_by("line_number")]


def item(order, number):
    return order.items.get(item_number=number)


# --- Purchase orders -------------------------------------------------------------------------

def test_order_defaults(demo):
    order = make_order(demo, release=False)
    assert (order.order_number, order.status, order.net_total) == ("PO00000001", "OPEN", Decimal("12450.00"))
    first = item(order, 10)
    assert (first.plant.code, first.storage_location.code, first.tax_code.code, first.unit.code) == (
        "1000", "0001", "I1", "EA",
    )
    assert str(order) == "PO00000001 Highveld Supplies CC"
    assert str(first) == "PO00000001/10 TG-1001 x 50.000"
    assert purchasing.release_order(order).status == "RELEASED"


@pytest.mark.parametrize("change, message", [
    ({"items": []}, "at least one item"),
    ({"quantity": Decimal("0")}, "greater than zero"),
    ({"net_price": Decimal("-1")}, "price of zero or more"),
    ({"tax_code": "O1"}, "not an input tax code"),
    ({"plant": "other"}, "does not belong"),
    ({"storage_location": "other"}, "is not in plant"),
    ({"material": "inactive"}, "not active"),
    ({"material": "service"}, "is a service"),
])
def test_order_item_rules(demo, change, message):
    from apps.organization.models import CompanyCode

    goods = material("TG-1001")
    kwargs = {"quantity": Decimal("1"), "net_price": Decimal("10")}
    if change.get("tax_code"):
        kwargs["tax_code"] = demo.tax(change["tax_code"])
    if change.get("quantity") is not None:
        kwargs["quantity"] = change["quantity"]
    if change.get("net_price") is not None:
        kwargs["net_price"] = change["net_price"]
    if change.get("plant"):
        other = CompanyCode.objects.create(code="2000", name="Other", currency=demo.company.currency,
                                           chart_of_accounts=demo.chart)
        kwargs["plant"] = Plant.objects.create(code="2000", name="Cape Town", company_code=other)
    if change.get("storage_location"):
        plant = Plant.objects.create(code="1100", name="Durban", company_code=demo.company)
        kwargs["storage_location"] = StorageLocation.objects.create(plant=plant, code="0001", name="Store")
    if change.get("material") == "inactive":
        Material.objects.filter(pk=goods.pk).update(is_active=False)
        goods.refresh_from_db()
    if change.get("material") == "service":
        goods = Material.objects.create(number="SV-1", description="Installation", base_unit=goods.base_unit,
                                        material_type=Material.MaterialType.SERVICE)
    items = change["items"] if "items" in change else [OrderItemInput(goods, **kwargs)]
    with pytest.raises(PurchasingError, match=message):
        make_order(demo, items=items)
    assert PurchaseOrder.objects.count() == 0


def test_order_needs_a_supplier_that_is_not_blocked(demo):
    with pytest.raises(PurchasingError, match="not a supplier"):
        make_order(demo, supplier=demo.customer)
    demo.supplier.is_blocked = True
    demo.supplier.save()
    with pytest.raises(PurchasingError, match="is blocked"):
        make_order(demo)


def test_purchasing_must_be_configured(demo):
    PurchasingAccountSetting.objects.all().delete()
    with pytest.raises(PurchasingError, match="not set up"):
        make_order(demo)


def test_items_change_only_while_open(demo):
    order = make_order(demo, release=False)
    purchasing.replace_order_items(order, [OrderItemInput(material("TG-1002"), Decimal("5"), Decimal("300"))])
    assert order.net_total == Decimal("1500.00")
    purchasing.release_order(order)
    with pytest.raises(PurchasingError, match="only change while the order is OPEN"):
        purchasing.replace_order_items(order, [OrderItemInput(material("TG-1002"), Decimal("1"), Decimal("1"))])
    with pytest.raises(PurchasingError, match="Only OPEN orders can be released"):
        purchasing.release_order(order)


def test_cancelling(demo):
    order = make_order(demo)
    receive(order, [ReceiptItemInput(item(order, 10), Decimal("5"))])
    with pytest.raises(PurchasingError, match="reverse them before cancelling"):
        purchasing.cancel_order(order)
    purchasing.reverse_goods_receipt(GoodsReceipt.objects.get())
    assert purchasing.cancel_order(order).status == "CANCELLED"
    with pytest.raises(PurchasingError, match="cannot be cancelled"):
        purchasing.cancel_order(order)
    with pytest.raises(PurchasingError, match="RELEASED orders"):
        receive(order)


# --- Goods receipts --------------------------------------------------------------------------

def test_goods_receipt_values_stock_at_the_order_price(demo):
    order = make_order(demo)
    receipt = receive(order)
    assert (receipt.receipt_number, receipt.material_document.document_number) == ("GR00000001", "MD00000002")
    entry = receipt.material_document.journal_entry
    assert (entry.document_number, entry.document_type) == ("WE00000002", "WE")
    assert lines(entry) == [("1200", Decimal("6250.00")), ("2300", Decimal("-6250.00")),
                            ("1200", Decimal("6200.00")), ("2300", Decimal("-6200.00"))]
    assert entry.lines.get(line_number=2).business_partner == demo.supplier
    assert [i.value for i in receipt.items.all()] == [Decimal("6250.00"), Decimal("6200.00")]

    plant = Plant.objects.get(code="1000")
    quantity, value = inventory.plant_stock(material("TG-1001"), plant)
    assert (quantity, value) == (Decimal("150.000"), Decimal("18250.00"))
    assert inventory.moving_average_price(quantity, value) == Decimal("121.6667")
    assert inventory.stock_reconciliation(demo.company)["reconciled"] is True
    assert item(order, 10).open_quantity == 0
    assert str(receipt) == "GR00000001 for PO00000001"


def test_partial_receipts_and_over_delivery(demo):
    order = make_order(demo)
    other_store = StorageLocation.objects.get(code="0002")
    receive(order, [ReceiptItemInput(item(order, 10), Decimal("30"), other_store)])
    assert inventory.location_stock(material("TG-1001"), other_store) == Decimal("30.000")
    with pytest.raises(PurchasingError, match="exceeds the open quantity 20.000"):
        receive(order, [ReceiptItemInput(item(order, 10), Decimal("21"))])
    second = receive(order)  # the rest: 20 x TG-1001 and 20 x TG-1002
    assert [(i.order_item.item_number, i.quantity) for i in second.items.all()] == [
        (10, Decimal("20.000")), (20, Decimal("20.000")),
    ]
    with pytest.raises(PurchasingError, match="Nothing left to receive"):
        receive(order)


def test_receipt_input_rules(demo):
    order, other = make_order(demo), make_order(demo)
    with pytest.raises(PurchasingError, match="does not belong to order"):
        receive(order, [ReceiptItemInput(item(other, 10), Decimal("1"))])
    with pytest.raises(PurchasingError, match="greater than zero"):
        receive(order, [ReceiptItemInput(item(order, 10), Decimal("0"))])
    durban = Plant.objects.create(code="1100", name="Durban", company_code=demo.company)
    StorageLocation.objects.create(plant=durban, code="0001", name="Store")
    split = make_order(demo, items=[OrderItemInput(material("TG-1001"), Decimal("1"), Decimal("1")),
                                    OrderItemInput(material("TG-1002"), Decimal("1"), Decimal("1"), plant=durban)])
    with pytest.raises(PurchasingError, match="one plant"):
        receive(split)


def test_reversing_a_receipt(demo):
    order = make_order(demo)
    receipt = receive(order)
    purchasing.reverse_goods_receipt(receipt)
    receipt.refresh_from_db()
    assert receipt.status == "REVERSED"
    assert item(order, 10).received_quantity == 0
    assert purchasing.gr_ir_report(demo.company)["ledger_balance"] == 0
    with pytest.raises(PurchasingError, match="already reversed"):
        purchasing.reverse_goods_receipt(receipt)


# --- Supplier invoices: the three-way match --------------------------------------------------

def test_invoice_at_order_price_clears_gr_ir(demo):
    order = make_order(demo)
    receive(order)
    invoice = bill(order)
    assert (invoice.invoice_number, invoice.net_amount, invoice.tax_amount, invoice.gross_amount) == (
        "IV00000001", Decimal("12450.00"), Decimal("1867.50"), Decimal("14317.50"),
    )
    assert invoice.due_date == date(2026, 6, 14)  # 30-day terms
    entry = invoice.journal_entry
    assert (entry.document_number, entry.document_type, entry.reference) == ("KR00000001", "KR", "HS-9001")
    assert lines(entry) == [
        ("2000", Decimal("-14317.50")),
        ("2300", Decimal("6250.00")), ("1300", Decimal("937.50")),
        ("2300", Decimal("6200.00")), ("1300", Decimal("930.00")),
    ]
    report = purchasing.gr_ir_report(demo.company)
    assert (report["items"], report["ledger_balance"], report["reconciled"]) == ([], Decimal("0.00"), True)
    order.refresh_from_db()
    assert order.status == "COMPLETED"
    assert purchasing.supplier_invoice_open_amount(invoice) == Decimal("14317.50")
    assert str(invoice) == "IV00000001 (HS-9001) Highveld Supplies CC 14317.50"


def test_price_difference_within_tolerance(demo):
    order = make_order(demo)
    receive(order)
    higher = bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("50"), Decimal("130.00"))])  # +4%
    assert lines(higher.journal_entry) == [
        ("2000", Decimal("-7475.00")),
        ("2300", Decimal("6250.00")), ("1300", Decimal("937.50")),
        ("5300", Decimal("250.00")), ("1300", Decimal("37.50")),
    ]
    row = higher.items.get()
    assert (row.net_amount, row.gr_ir_amount, row.price_difference, row.tax_amount) == (
        Decimal("6500.00"), Decimal("6250.00"), Decimal("250.00"), Decimal("975.00"),
    )
    lower = bill(order, "HS-9002", items=[InvoiceItemInput(item(order, 20), Decimal("20"), Decimal("300.00"))])
    assert lines(lower.journal_entry) == [
        ("2000", Decimal("-6900.00")),
        ("2300", Decimal("6200.00")), ("1300", Decimal("930.00")),
        ("5300", Decimal("-200.00")), ("1300", Decimal("-30.00")),
    ]
    assert lower.tax_amount == Decimal("900.00")
    assert purchasing.gr_ir_report(demo.company)["ledger_balance"] == 0


def test_price_outside_tolerance_is_rejected(demo):
    order = make_order(demo)
    receive(order)
    with pytest.raises(PurchasingError, match="by more than 5.00%"):
        bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("50"), Decimal("132.00"))])  # +5.6%
    PurchasingAccountSetting.objects.update(price_tolerance_percent=Decimal("10"))
    assert bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("50"), Decimal("132.00"))]).pk


def test_cannot_invoice_more_than_received(demo):
    order = make_order(demo)
    with pytest.raises(PurchasingError, match="Nothing has been received"):
        bill(order)
    receive(order, [ReceiptItemInput(item(order, 10), Decimal("30"))])
    with pytest.raises(PurchasingError, match="only 30.000 has been received"):
        bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("40"))])
    with pytest.raises(PurchasingError, match="only 0.000 has been received"):
        bill(order, items=[InvoiceItemInput(item(order, 20), Decimal("1"))])
    invoice = bill(order)  # defaults to what is received and not yet invoiced
    assert [(i.order_item.item_number, i.quantity) for i in invoice.items.all()] == [(10, Decimal("30.000"))]
    report = purchasing.gr_ir_report(demo.company)
    assert report["items"] == [] and report["reconciled"] is True
    order.refresh_from_db()
    assert order.status == "RELEASED"  # 20 + 20 still to come


def test_invoice_input_rules(demo):
    order, other = make_order(demo), make_order(demo)
    receive(order)
    with pytest.raises(PurchasingError, match="supplier's invoice number"):
        bill(order, reference="  ")
    with pytest.raises(PurchasingError, match="does not belong to order"):
        bill(order, items=[InvoiceItemInput(item(other, 10), Decimal("1"))])
    with pytest.raises(PurchasingError, match="greater than zero"):
        bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("0"))])
    with pytest.raises(PurchasingError, match="only once"):
        bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("1")),
                           InvoiceItemInput(item(order, 10), Decimal("1"))])
    with pytest.raises(PurchasingError, match="by more than"):
        bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("1"), Decimal("-1"))])
    purchasing.cancel_order(other)
    with pytest.raises(PurchasingError, match="CANCELLED and cannot be invoiced"):
        bill(other)
    assert SupplierInvoice.objects.count() == 0


def test_free_items_and_zero_totals(demo):
    order = make_order(demo, items=[OrderItemInput(material("TG-1001"), Decimal("2"), Decimal("0"))])
    receive(order)
    with pytest.raises(PurchasingError, match="by more than"):
        bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("2"), Decimal("1"))])
    with pytest.raises(PurchasingError, match="total must be greater than zero"):
        bill(order)


def test_duplicate_supplier_invoice_numbers_are_rejected(demo):
    order = make_order(demo)
    receive(order)
    invoice = bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("10"))])
    with pytest.raises(PurchasingError, match="already posted"):
        bill(order, reference="hs-9001", items=[InvoiceItemInput(item(order, 10), Decimal("10"))])
    purchasing.cancel_supplier_invoice(invoice)
    assert bill(order, items=[InvoiceItemInput(item(order, 10), Decimal("10"))]).invoice_number == "IV00000002"


def test_split_receipts_and_invoices_leave_no_cents_on_gr_ir(demo):
    order = make_order(demo, items=[OrderItemInput(material("TG-1001"), Decimal("1"), Decimal("10.01"))])
    half = Decimal("0.5")
    receive(order, [ReceiptItemInput(item(order, 10), half)])
    receive(order, [ReceiptItemInput(item(order, 10), half)])
    assert item(order, 10).received_value == Decimal("10.02")  # 5.005 rounds up twice
    bill(order, items=[InvoiceItemInput(item(order, 10), half)])
    last = bill(order, "HS-9002")
    assert (last.items.get().gr_ir_amount, last.items.get().price_difference) == (Decimal("5.01"), Decimal("0.00"))
    assert purchasing.gr_ir_report(demo.company)["ledger_balance"] == 0


def test_receipt_cannot_be_reversed_once_invoiced(demo):
    order = make_order(demo)
    receipt = receive(order)
    invoice = bill(order)
    with pytest.raises(PurchasingError, match="cancel the supplier invoice first"):
        purchasing.reverse_goods_receipt(receipt)
    purchasing.cancel_supplier_invoice(invoice)
    order.refresh_from_db()
    assert order.status == "RELEASED"
    assert purchasing.gr_ir_report(demo.company)["open_balance"] == Decimal("12450.00")
    purchasing.reverse_goods_receipt(receipt)
    assert purchasing.gr_ir_report(demo.company)["reconciled"] is True


def test_cancelling_an_invoice(demo):
    order = make_order(demo)
    receive(order)
    invoice = bill(order)
    cancelled = purchasing.cancel_supplier_invoice(invoice, posting_date=date(2026, 5, 20))
    assert cancelled.status == "CANCELLED"
    assert purchasing.supplier_invoice_open_amount(cancelled) == 0
    assert JournalEntry.objects.get(reversal_of=invoice.journal_entry).posting_date == date(2026, 5, 20)
    with pytest.raises(PurchasingError, match="already cancelled"):
        purchasing.cancel_supplier_invoice(invoice)


def test_paid_invoice_cannot_be_cancelled(demo):
    order = make_order(demo)
    receive(order)
    invoice = bill(order)
    payment = finance.post_outgoing_payment(company_code=demo.company, supplier=demo.supplier,
                                            payment_date=date(2026, 6, 10), amount=Decimal("1000.00"),
                                            bank_account=demo.account("1000"))
    assert purchasing.supplier_invoice_open_amount(invoice) == Decimal("13317.50")
    with pytest.raises(PurchasingError, match="has payments"):
        purchasing.cancel_supplier_invoice(invoice)
    finance.reverse_journal_entry(payment)
    assert purchasing.cancel_supplier_invoice(invoice).status == "CANCELLED"


def test_manual_posting_to_gr_ir_shows_in_the_report(demo):
    finance.post_journal_entry(company_code=demo.company, posting_date=RECEIPT_DAY, lines=[
        finance.LineInput(demo.account("6200"), "D", Decimal("40")),
        finance.LineInput(demo.account("2300"), "C", Decimal("40")),
    ])
    report = purchasing.gr_ir_report(demo.company)
    assert (report["ledger_balance"], report["difference"], report["reconciled"]) == (
        Decimal("40.00"), Decimal("-40.00"), False,
    )


def test_whole_cycle_keeps_the_books_balanced(demo):
    order = make_order(demo)
    receive(order)
    bill(order)
    finance.post_outgoing_payment(company_code=demo.company, supplier=demo.supplier, payment_date=date(2026, 6, 10),
                                  amount=Decimal("14317.50"), bank_account=demo.account("1000"))
    assert finance.open_items(demo.company, reconciliation_type="SUPPLIER") == []
    assert finance.trial_balance(demo.company, 2027)["balanced"] is True
    assert finance.payables_reconciliation(demo.company)["reconciled"] is True
    assert inventory.stock_reconciliation(demo.company)["reconciled"] is True


def test_purchasing_error_is_a_posting_error():
    assert issubclass(PurchasingError, PostingError)


def test_setting_string(demo):
    assert str(PurchasingAccountSetting.objects.get()) == "CAZA: PRD 5300, tolerance 5.00%"
