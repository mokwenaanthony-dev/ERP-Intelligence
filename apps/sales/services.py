"""Business rules for sales orders.

Kept out of serializers and views so the same rules apply whether an order is
changed through the API, the admin, a Celery task or a management command.
"""
from collections import defaultdict

from django.db import transaction
from django.db.models import F

from apps.inventory.models import Product

from .models import SalesOrder, SalesOrderLine


class OrderRuleError(Exception):
    """A requested change breaks a sales-order business rule."""


def _quantities_by_product(order: SalesOrder) -> dict:
    # Query the lines fresh rather than via order.lines.all(): a prefetched
    # cache would be stale if the lines were replaced in the same request.
    totals = defaultdict(int)
    for line in SalesOrderLine.objects.filter(order=order):
        totals[line.product_id] += line.quantity
    return totals


def _reserve_stock(order: SalesOrder) -> None:
    needed = _quantities_by_product(order)
    if not needed:
        raise OrderRuleError("An order needs at least one line before it can be confirmed.")

    # Lock the product rows so two orders cannot take the same stock at once.
    products = Product.objects.select_for_update().filter(pk__in=needed).order_by("pk")
    shortages = [
        f"{product.sku}: need {needed[product.pk]}, have {product.stock}"
        for product in products
        if product.stock < needed[product.pk]
    ]
    if shortages:
        raise OrderRuleError("Insufficient stock - " + "; ".join(shortages))

    for product_id, quantity in needed.items():
        Product.objects.filter(pk=product_id).update(stock=F("stock") - quantity)


def _release_stock(order: SalesOrder) -> None:
    for product_id, quantity in _quantities_by_product(order).items():
        Product.objects.filter(pk=product_id).update(stock=F("stock") + quantity)


@transaction.atomic
def change_status(order: SalesOrder, new_status: str, user=None) -> SalesOrder:
    """Move an order to ``new_status``, adjusting stock where required.

    * PENDING -> CONFIRMED takes the ordered quantities out of stock.
    * CONFIRMED -> CANCELLED puts them back.
    """
    if new_status == order.status:
        return order
    if not order.can_transition_to(new_status):
        raise OrderRuleError(f"An order cannot move from {order.status} to {new_status}.")

    if new_status == SalesOrder.Status.CONFIRMED:
        _reserve_stock(order)
    elif new_status == SalesOrder.Status.CANCELLED and order.status == SalesOrder.Status.CONFIRMED:
        _release_stock(order)

    order.status = new_status
    if user is not None:
        order.updated_by = user
    order.save(update_fields=["status", "updated_by", "updated_at"])
    return order
