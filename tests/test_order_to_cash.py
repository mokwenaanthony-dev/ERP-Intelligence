"""Order-to-cash business rules (apps.sales.services and the receivables side of apps.finance.services).

Demo data used: customer Acme Retail with a customer role (account 1100, 30-day terms) and an open
sample invoice DR00000001 of R11,500 dated 2026-03-01; TG-1001 (100 in stock at R120, price R199)
and TG-1002 (50 in stock at R300, price R499) in plant 1000, storage location 0001.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.finance import services as finance
from apps.finance.models import JournalEntry, TaxCode
from apps.inventory import services as inventory
from apps.inventory.services import StockError
from apps.masterdata.models import BusinessPartner, Material, UnitOfMeasure
from apps.organization.models import Plant, SalesOrganization, StorageLocation
from apps.sales import services
from apps.sales.models import Delivery, Invoice, RevenueAccountSetting, SalesOrder, SalesPrice
from apps.sales.services import DeliveryItemInput, OrderItemInput, SalesError

pytestmark = pytest.mark.django_db

ORDER_DAY, DELIVERY_DAY, INVOICE_DAY, PAYMENT_DAY = (
    date(2026, 5, 10), date(2026, 5, 12), date(2026, 5, 15), date(2026, 6, 10),
)


@pytest.fixture
def o2c(demo):
    plant = Plant.objects.get(code="1000")
    return SimpleNamespace(
        demo=demo, plant=plant, sales_org=SalesOrganization.objects.get(code="1000"),
        main=StorageLocation.objects.get(plant=plant, code="0001"),
        widget=Material.objects.get(number="TG-1001"), gadget=Material.objects.get(number="TG-1002"),
        customer=demo.customer, bank=demo.account("1000"),
    )


def make_order(o2c, *items, confirm=True, **kwargs):
    items = items or (OrderItemInput(o2c.widget, Decimal("10")), OrderItemInput(o2c.gadget, Decimal("2")))
    order = services.create_sales_order(sales_organization=o2c.sales_org, customer=o2c.customer,
                                        order_date=ORDER_DAY, items=list(items), **kwargs)
    return services.confirm_order(order) if confirm else order


def deliver_all(o2c, order):
    return services.post_delivery(order, delivery_date=DELIVERY_DAY, storage_location=o2c.main)


def lines(entry):
    return [(line.gl_account.number, line.amount) for line in entry.lines.order_by("line_number")]


# --- The happy path, end to end ----------------------------------------------------------------

def test_full_order_to_cash_cycle(o2c, make_user):
    user = make_user(username="clerk")
    order = make_order(o2c, customer_reference="PO-4471")
    assert (order.order_number, order.status, order.net_total) == ("SO00000001", "CONFIRMED", Decimal("2988.00"))
    assert [(i.item_number, i.net_price, i.tax_code.code) for i in order.items.all()] == [
        (10, Decimal("199.00"), "O1"), (20, Decimal("499.00"), "O1"),
    ]

    # Delivery: 601 goods issue at moving average cost -> Dr cost of sales / Cr inventory
    delivery = services.post_delivery(order, delivery_date=DELIVERY_DAY, storage_location=o2c.main, user=user)
    assert delivery.delivery_number == "DL00000001"
    goods_issue = delivery.material_document
    assert (goods_issue.movement_type, goods_issue.business_partner) == ("601", o2c.customer)
    assert lines(goods_issue.journal_entry) == [
        ("1200", Decimal("-1200.00")), ("5000", Decimal("1200.00")),
        ("1200", Decimal("-600.00")), ("5000", Decimal("600.00")),
    ]
    assert inventory.plant_stock(o2c.widget, o2c.plant) == (Decimal("90.000"), Decimal("10800.00"))

    # Invoice: Dr receivables gross / Cr revenue net / Cr VAT per item
    invoice = services.post_invoice(delivery, invoice_date=INVOICE_DAY, user=user)
    assert (invoice.invoice_number, invoice.net_amount, invoice.tax_amount, invoice.gross_amount) == (
        "IN00000001", Decimal("2988.00"), Decimal("448.20"), Decimal("3436.20"),
    )
    assert invoice.due_date == date(2026, 6, 14)
    assert invoice.journal_entry.document_number == "DR00000002"
    assert lines(invoice.journal_entry) == [
        ("1100", Decimal("3436.20")), ("4000", Decimal("-1990.00")), ("2100", Decimal("-298.50")),
        ("4000", Decimal("-998.00")), ("2100", Decimal("-149.70")),
    ]
    order.refresh_from_db()
    assert order.status == "COMPLETED"
    item = order.items.get(item_number=10)
    assert (item.delivered_quantity, item.invoiced_quantity, item.open_quantity) == (10, 10, 0)

    # Payment clears the invoice
    receivable = invoice.journal_entry.lines.get(line_number=1)
    payment = finance.post_incoming_payment(
        company_code=o2c.demo.company, customer=o2c.customer, payment_date=PAYMENT_DAY, amount=Decimal("3436.20"),
        bank_account=o2c.bank, allocations=[(receivable.pk, Decimal("3436.20"))], reference="EFT-991", user=user,
    )
    assert payment.document_number == "DZ00000001"
    assert lines(payment) == [("1000", Decimal("3436.20")), ("1100", Decimal("-3436.20"))]
    assert services.invoice_open_amount(invoice) == 0
    assert finance.receivables_reconciliation(o2c.demo.company)["reconciled"] is True


# --- Sales orders ------------------------------------------------------------------------------

def test_manual_price_overrides_the_price_list(o2c):
    order = make_order(o2c, OrderItemInput(o2c.widget, Decimal("1"), net_price=Decimal("150")))
    item = order.items.get()
    assert (item.net_price, item.price_overridden) == (Decimal("150.00"), True)


def test_latest_valid_price_is_used(o2c):
    SalesPrice.objects.create(material=o2c.widget, sales_organization=o2c.sales_org, price=Decimal("219.00"),
                              valid_from=date(2026, 5, 1))
    assert make_order(o2c, OrderItemInput(o2c.widget, Decimal("1"))).items.get().net_price == Decimal("219.00")
    assert services.price_for(o2c.widget, o2c.sales_org, date(2026, 4, 30)) == Decimal("199.00")


def test_order_rules(o2c):
    unit = UnitOfMeasure.objects.get(code="EA")
    unpriced = Material.objects.create(number="TG-9", description="No price", base_unit=unit)
    inactive = Material.objects.create(number="TG-8", description="Old", base_unit=unit, is_active=False)
    blocked = BusinessPartner.objects.create(name="Blocked customer", is_blocked=True)
    blocked.customer_roles.create(company_code=o2c.demo.company, reconciliation_account=o2c.demo.account("1100"))
    input_vat = TaxCode.objects.get(code="I1")
    other_company_plant = Plant(code="9", company_code_id=-1)

    def create(customer=None, items=None):
        return services.create_sales_order(sales_organization=o2c.sales_org, customer=customer or o2c.customer,
                                           order_date=ORDER_DAY, items=items)

    cases = [
        ({"customer": o2c.demo.supplier, "items": [OrderItemInput(o2c.widget, Decimal("1"))]}, "not a customer"),
        ({"customer": blocked, "items": [OrderItemInput(o2c.widget, Decimal("1"))]}, "is blocked"),
        ({"items": []}, "at least one item"),
        ({"items": [OrderItemInput(unpriced, Decimal("1"))]}, "No price for TG-9"),
        ({"items": [OrderItemInput(inactive, Decimal("1"))]}, "not active"),
        ({"items": [OrderItemInput(o2c.widget, Decimal("0"))]}, "greater than zero"),
        ({"items": [OrderItemInput(o2c.widget, Decimal("1"), net_price=Decimal("-1"))]}, "cannot be negative"),
        ({"items": [OrderItemInput(o2c.widget, Decimal("1"), tax_code=input_vat)]}, "not an output tax code"),
        ({"items": [OrderItemInput(o2c.widget, Decimal("1"), plant=other_company_plant)]}, "does not belong"),
    ]
    for kwargs, message in cases:
        with pytest.raises(SalesError, match=message):
            create(**kwargs)
    assert SalesOrder.objects.count() == 0


def test_missing_revenue_account_is_reported(o2c):
    RevenueAccountSetting.objects.all().delete()
    with pytest.raises(SalesError, match="No revenue account"):
        make_order(o2c)


def test_items_change_only_while_open(o2c):
    order = make_order(o2c, confirm=False)
    services.replace_order_items(order, [OrderItemInput(o2c.gadget, Decimal("3"))])
    assert [(i.item_number, i.material, i.quantity) for i in order.items.all()] == [(10, o2c.gadget, 3)]
    services.confirm_order(order)
    with pytest.raises(SalesError, match="only change while the order is OPEN"):
        services.replace_order_items(order, [OrderItemInput(o2c.widget, Decimal("1"))])
    with pytest.raises(SalesError, match="Only OPEN orders can be confirmed"):
        services.confirm_order(order)


def test_cancelling_orders(o2c):
    assert services.cancel_order(make_order(o2c, confirm=False)).status == "CANCELLED"
    delivered = make_order(o2c)
    deliver_all(o2c, delivered)
    with pytest.raises(SalesError, match="has deliveries"):
        services.cancel_order(delivered)
    services.post_invoice(delivered.deliveries.get(), invoice_date=INVOICE_DAY)
    with pytest.raises(SalesError, match="COMPLETED order cannot be cancelled"):
        services.cancel_order(delivered)


# --- Deliveries ------------------------------------------------------------------------------

def test_partial_deliveries(o2c):
    order = make_order(o2c)
    widget_item, gadget_item = order.items.all()
    first = services.post_delivery(order, delivery_date=DELIVERY_DAY,
                                   items=[DeliveryItemInput(widget_item, Decimal("4"), o2c.main)])
    assert widget_item.open_quantity == 6
    second = deliver_all(o2c, order)
    assert [(i.order_item.item_number, i.quantity) for i in second.items.all()] == [(10, 6), (20, 2)]
    assert (first.delivery_number, second.delivery_number) == ("DL00000001", "DL00000002")
    with pytest.raises(SalesError, match="Nothing left to deliver"):
        deliver_all(o2c, order)


def test_delivery_rules(o2c):
    order = make_order(o2c)
    other_order = make_order(o2c)
    widget_item = order.items.get(item_number=10)
    with pytest.raises(SalesError, match="Only CONFIRMED orders"):
        deliver_all(o2c, make_order(o2c, confirm=False))
    with pytest.raises(SalesError, match="exceeds the open quantity"):
        services.post_delivery(order, delivery_date=DELIVERY_DAY,
                               items=[DeliveryItemInput(widget_item, Decimal("11"), o2c.main)])
    with pytest.raises(SalesError, match="greater than zero"):
        services.post_delivery(order, delivery_date=DELIVERY_DAY,
                               items=[DeliveryItemInput(widget_item, Decimal("0"), o2c.main)])
    with pytest.raises(SalesError, match="does not belong to order"):
        services.post_delivery(order, delivery_date=DELIVERY_DAY,
                               items=[DeliveryItemInput(other_order.items.first(), Decimal("1"), o2c.main)])
    with pytest.raises(SalesError, match="storage location to deliver everything"):
        services.post_delivery(order, delivery_date=DELIVERY_DAY)


def test_one_delivery_ships_from_one_plant(o2c):
    durban = Plant.objects.create(code="2000", name="Durban", company_code=o2c.demo.company)
    order = make_order(o2c, OrderItemInput(o2c.widget, Decimal("1")),
                       OrderItemInput(o2c.widget, Decimal("1"), plant=durban))
    with pytest.raises(SalesError, match="one plant"):
        deliver_all(o2c, order)


def test_delivery_without_stock_posts_nothing(o2c):
    order = make_order(o2c, OrderItemInput(o2c.widget, Decimal("500")))
    with pytest.raises(StockError, match="Insufficient stock"):
        deliver_all(o2c, order)
    assert Delivery.objects.count() == 0
    assert inventory.plant_stock(o2c.widget, o2c.plant)[0] == 100


def test_reversing_a_delivery_returns_the_stock(o2c):
    order = make_order(o2c)
    delivery = services.reverse_delivery(deliver_all(o2c, order))
    assert delivery.status == "REVERSED"
    assert inventory.plant_stock(o2c.widget, o2c.plant) == (Decimal("100.000"), Decimal("12000.00"))
    assert order.items.get(item_number=10).open_quantity == 10
    with pytest.raises(SalesError, match="already reversed"):
        services.reverse_delivery(delivery)
    with pytest.raises(SalesError, match="reversed and cannot be invoiced"):
        services.post_invoice(delivery, invoice_date=INVOICE_DAY)
    assert deliver_all(o2c, order).delivery_number == "DL00000002"  # can be delivered again


def test_invoiced_delivery_cannot_be_reversed(o2c):
    delivery = deliver_all(o2c, make_order(o2c))
    services.post_invoice(delivery, invoice_date=INVOICE_DAY)
    with pytest.raises(SalesError, match="has been invoiced"):
        services.reverse_delivery(delivery)


# --- Invoices --------------------------------------------------------------------------------

def test_delivery_is_invoiced_once(o2c):
    delivery = deliver_all(o2c, make_order(o2c))
    services.post_invoice(delivery, invoice_date=INVOICE_DAY)
    with pytest.raises(SalesError, match="already been invoiced"):
        services.post_invoice(delivery, invoice_date=INVOICE_DAY)


def test_cancelling_an_invoice_reverses_it_and_reopens_the_order(o2c):
    order = make_order(o2c)
    delivery = deliver_all(o2c, order)
    invoice = services.cancel_invoice(services.post_invoice(delivery, invoice_date=INVOICE_DAY))
    assert invoice.status == "CANCELLED"
    invoice.journal_entry.refresh_from_db()
    assert invoice.journal_entry.status == "REVERSED"
    order.refresh_from_db()
    assert order.status == "CONFIRMED"
    assert services.invoice_open_amount(invoice) == 0
    with pytest.raises(SalesError, match="already cancelled"):
        services.cancel_invoice(invoice)
    assert services.post_invoice(delivery, invoice_date=INVOICE_DAY).invoice_number == "IN00000002"
    assert finance.receivables_reconciliation(o2c.demo.company)["reconciled"] is True


def test_paid_invoice_cannot_be_cancelled(o2c):
    invoice = services.post_invoice(deliver_all(o2c, make_order(o2c)), invoice_date=INVOICE_DAY)
    receivable = invoice.journal_entry.lines.get(line_number=1)
    finance.post_incoming_payment(company_code=o2c.demo.company, customer=o2c.customer, payment_date=PAYMENT_DAY,
                                  amount=Decimal("100"), bank_account=o2c.bank,
                                  allocations=[(receivable.pk, Decimal("100"))])
    with pytest.raises(SalesError, match="has payments"):
        services.cancel_invoice(invoice)
    assert services.invoice_open_amount(invoice) == Decimal("3336.20")


def test_string_representations(o2c):
    order = make_order(o2c)
    delivery = deliver_all(o2c, order)
    invoice = services.post_invoice(delivery, invoice_date=INVOICE_DAY)
    assert str(order) == "SO00000001 Acme Retail (Pty) Ltd"
    assert str(order.items.first()) == "SO00000001/10 TG-1001 x 10.000"
    assert str(delivery) == "DL00000001 for SO00000001"
    assert str(delivery.items.first()) == "DL00000001: TG-1001 x 10.000"
    assert str(invoice) == "IN00000001 Acme Retail (Pty) Ltd 3436.20"
    assert str(invoice.items.first()) == "IN00000001: TG-1001 x 10.000"
    assert str(SalesPrice.objects.first()).startswith("TG-1001 @ 1000: 199.00")
    assert str(RevenueAccountSetting.objects.get(material_type="")) == "CAZA all -> 4000"
    assert JournalEntry.objects.filter(document_type="DR").count() == 2  # demo sample + this invoice
    assert Invoice.objects.count() == 1
