"""Publishing ERP state to the Unified Namespace from the transactional outbox.

Every save of a work centre or a production order already writes an outbox event in the same
transaction (ADR 0003). This module reads those events in order, after its own cursor, and publishes
the record's **current state** as retained messages:

* a work centre -> ``<cell>/definition/equipment`` (cleared if the work centre is deleted);
* a released order -> one ``<cell>/schedule/operation-requests/<order>-<op>`` per operation;
  a completed or cancelled order clears those requests (empty retained payload).

State-based and retained, so publishing twice changes nothing (ADR 0010), and a message that was
lost is repaired by the next change or by ``uns_republish``. If the broker is unreachable, the
cursor stays where it was and the events are published on the next run.
"""
import logging

from django.conf import settings
from django.db import transaction

from apps.graph.models import OutboxEvent
from apps.production.models import ProductionOrder, WorkCenter

from . import contract
from .broker import publisher_broker
from .models import OutboxCursor

logger = logging.getLogger(__name__)

CURSOR = "uns"


def equipment_data(wc: WorkCenter) -> dict:
    """The ``equipment/v1`` data of a work centre."""
    line = wc.line
    return {
        "company_code": wc.plant.company_code.code, "plant": wc.plant.code, "area": line.area.code,
        "line": line.code, "work_center": wc.code, "name": wc.name, "isa95_level": wc.isa95_level,
        "cost_center": wc.cost_center.code, "activity_types": sorted(a.code for a in wc.activity_types.all()),
        "is_active": wc.is_active, "uns_path": wc.uns_path,
    }


def operation_request_data(order: ProductionOrder, operation) -> dict:
    """The ``operation-request/v1`` data of one operation of an order."""
    return {
        "order": order.order_number, "operation_number": operation.operation_number,
        "description": operation.description, "material": order.material.number,
        "material_description": order.material.description, "quantity": order.quantity,
        "unit": order.unit.code, "start_date": order.start_date, "finish_date": order.finish_date,
        "status": order.status,
        "planned_activities": {a.activity_type.code: a.planned_quantity for a in operation.activities.all()},
    }


def publish_work_center(broker, wc: WorkCenter, event_id=None) -> int:
    """Publish (retained) what a work centre is."""
    broker.publish(contract.equipment_topic(wc.uns_path),
                   contract.envelope(contract.EQUIPMENT, equipment_data(wc), event_id), retain=True)
    return 1


def publish_order(broker, order: ProductionOrder, event_id=None) -> int:
    """Publish a released order's operation requests, or clear them once it is completed or cancelled."""
    released = order.status == ProductionOrder.Status.RELEASED
    clear = order.status in (ProductionOrder.Status.COMPLETED, ProductionOrder.Status.CANCELLED)
    if not (released or clear):
        return 0  # a created order is not yet work for the shop floor
    count = 0
    for operation in order.operations.select_related("work_center__line__area__plant__company_code") \
            .prefetch_related("activities__activity_type"):
        topic = contract.operation_request_topic(operation.work_center.uns_path, order.order_number,
                                                 operation.operation_number)
        payload = contract.envelope(contract.OPERATION_REQUEST, operation_request_data(order, operation),
                                    event_id) if released else b""
        broker.publish(topic, payload, retain=True)
        count += 1
    return count


def _publish_event(broker, event: OutboxEvent) -> int:
    event_id = f"erp-outbox-{event.pk}"
    if event.aggregate_type == "production.workcenter":
        if event.payload.get("deleted"):
            path = event.payload.get("uns_path")
            if path:
                broker.publish(contract.equipment_topic(path), b"", retain=True)
                return 1
            return 0
        wc = WorkCenter.objects.select_related("plant__company_code", "line__area__plant__company_code",
                                               "cost_center").filter(pk=event.aggregate_id).first()
        return publish_work_center(broker, wc, event_id) if wc else 0
    if event.aggregate_type == "production.productionorder":
        order = ProductionOrder.objects.select_related("material", "unit").filter(pk=event.aggregate_id).first()
        return publish_order(broker, order, event_id) if order else 0
    return 0  # other changes are not part of the contract


def publish_outbox(limit: int = 500, broker=None) -> dict:
    """Publish the outbox events after the cursor, in order; runs every 5 seconds from Celery beat."""
    if broker is None:
        if not settings.UNS_ENABLED:
            return {"enabled": False}
        broker = publisher_broker()
    with transaction.atomic():
        cursor, _ = OutboxCursor.objects.select_for_update().get_or_create(name=CURSOR)
        events = list(OutboxEvent.objects.filter(pk__gt=cursor.last_event_id).order_by("pk")[:limit])
        messages, handled = 0, 0
        for event in events:
            try:
                messages += _publish_event(broker, event)
            except Exception as exc:  # broker down: keep the cursor here and retry on the next run
                logger.error("UNS publish stopped at outbox event %s (%s %s); will retry", event.pk,
                             event.aggregate_type, event.aggregate_id, exc_info=exc)
                break
            cursor.last_event_id = event.pk
            handled += 1
        cursor.save(update_fields=["last_event_id", "updated_at"])
    if handled:
        logger.info("UNS: %d outbox event(s) handled, %d message(s) published", handled, messages)
    return {"enabled": True, "events": handled, "messages": messages, "cursor": cursor.last_event_id}


def republish(broker=None) -> int:
    """Publish the current state of every work centre and every released order (``uns_republish``).

    For a new or emptied broker. Safe at any time: retained state published again changes nothing.
    """
    broker = broker or publisher_broker()
    count = 0
    for wc in WorkCenter.objects.select_related("plant__company_code", "line__area__plant__company_code",
                                                "cost_center").prefetch_related("activity_types"):
        count += publish_work_center(broker, wc)
    for order in ProductionOrder.objects.filter(status=ProductionOrder.Status.RELEASED).select_related(
            "material", "unit"):
        count += publish_order(broker, order)
    last = OutboxEvent.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    OutboxCursor.objects.update_or_create(name=CURSOR, defaults={"last_event_id": last})
    logger.info("UNS republished: %d retained message(s); cursor at outbox event %s", count, last)
    return count
