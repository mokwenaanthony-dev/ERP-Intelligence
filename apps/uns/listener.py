"""Receiving the MES's work: operation-performance events become confirmations.

``manage.py uns_listen`` subscribes to ``+/+/+/+/+/events/operation-performance`` and hands every
message to ``handle_performance``, which:

1. checks the topic and the envelope (unknown schemas are logged and ignored, as the contract says);
2. finds the order from the topic's company code and plant and the message's order number, and
   checks the operation really runs at the cell that reported it;
3. posts it with ``orders.confirm_operation(..., external_id=event_id)``. The event ID is the
   idempotency key (ADR 0010): the same event again returns the confirmation posted the first time;
4. answers on ``<company>/<plant>/erp/events/confirmation-result`` with POSTED (``replayed`` true for
   a repeat) or REJECTED and the business rule's message, so the MES knows it can stop resending.
"""
import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import close_old_connections

from apps.common.logcontext import request_id
from apps.finance.services import PostingError
from apps.production import orders
from apps.production.models import ProductionOrder

from . import contract

logger = logging.getLogger(__name__)


class MessageError(ValueError):
    """A performance message that does not follow the contract."""


def _decimal(value, name) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MessageError(f"{name} must be a decimal string, got {value!r}.") from exc


def _parse(topic: str, payload: bytes):
    """Topic parts and the message's data, or MessageError."""
    try:
        message = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MessageError("The payload is not JSON.") from exc
    if not isinstance(message, dict) or not isinstance(message.get("data"), dict):
        raise MessageError("The payload is not a contract envelope (no data object).")
    return message


def _confirm(levels: list, event_id: str, data: dict):
    """Post the event as a confirmation. Returns (confirmation, replayed)."""
    company, plant = levels[0], levels[1]
    try:
        order_number, operation_number = str(data["order"]), int(data["operation_number"])
        posting_date = date.fromisoformat(str(data["posting_date"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MessageError(f"Missing or invalid field: {exc}.") from exc
    order = (ProductionOrder.objects.filter(company_code__code__iexact=company, plant__code__iexact=plant,
                                            order_number__iexact=order_number)
             .select_related("plant").first())
    if order is None:
        raise MessageError(f"Order {order_number} not found in plant {plant.upper()}.")
    operation = order.operations.select_related("work_center__line__area__plant__company_code") \
        .filter(operation_number=operation_number).first()
    reported_at = "/".join(levels[:5])
    if operation is not None and operation.work_center.uns_path != reported_at:
        raise MessageError(f"Operation {operation_number} of order {order.order_number} runs at "
                           f"{operation.work_center.uns_path}, not {reported_at}.")
    activities = None
    if data.get("activities") is not None:
        if not isinstance(data["activities"], dict):
            raise MessageError("activities must be an object of activity type: quantity.")
        activities = {str(code): _decimal(qty, f"activities.{code}") for code, qty in data["activities"].items()}
    replayed = order.confirmations.filter(external_id=event_id).exists()
    confirmation = orders.confirm_operation(
        order, operation_number, posting_date,
        yield_quantity=_decimal(data.get("yield_quantity", "0"), "yield_quantity"),
        scrap_quantity=_decimal(data.get("scrap_quantity", "0"), "scrap_quantity"),
        activities=activities, final=bool(data.get("final", False)), external_id=event_id,
    )
    return confirmation, replayed


def handle_performance(broker, topic: str, payload: bytes) -> dict | None:
    """Post one operation-performance event and publish the result. Returns the result data, or None
    for a message that is not ours to answer (wrong topic or unknown schema)."""
    levels = topic.split("/")
    if len(levels) != 7 or levels[5:] != ["events", "operation-performance"]:
        logger.warning("Ignored message on unexpected topic %s", topic)
        return None
    event_id, message = None, {}
    token = request_id.set("uns")
    try:
        close_old_connections()  # this runs on the MQTT network thread; drop stale DB connections
        try:
            message = _parse(topic, payload)
            if message.get("schema") != contract.OPERATION_PERFORMANCE:
                logger.warning("Ignored message with unknown schema %r on %s", message.get("schema"), topic)
                return None
            event_id = message.get("event_id")
            if not event_id or not isinstance(event_id, str) or len(event_id) > 64:
                raise MessageError("event_id is required (at most 64 characters): it is the idempotency key.")
            request_id.set(f"uns:{event_id[-24:]}")
            confirmation, replayed = _confirm(levels, event_id, message["data"])
            result = {"event_id": event_id, "status": "POSTED", "order": confirmation.order.order_number,
                      "confirmation_number": confirmation.confirmation_number,
                      "journal_entry": getattr(confirmation.journal_entry, "document_number", None),
                      "reason": None, "replayed": replayed}
            logger.info("UNS event %s from %s: confirmation %s of order %s%s", event_id, "/".join(levels[:5]),
                        confirmation.confirmation_number, confirmation.order.order_number,
                        " (replayed)" if replayed else "")
        except (MessageError, PostingError) as exc:
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            result = {"event_id": event_id, "status": "REJECTED", "order": data.get("order"),
                      "confirmation_number": None, "journal_entry": None, "reason": str(exc), "replayed": False}
            logger.warning("UNS event %s from %s rejected: %s", event_id, "/".join(levels[:5]), exc)
        broker.publish(contract.confirmation_result_topic(levels[0], levels[1]),
                       contract.envelope(contract.CONFIRMATION_RESULT, result, event_id))
        return result
    finally:
        request_id.reset(token)


def listen(broker) -> None:
    """Subscribe to every cell's performance events; the caller keeps the process alive."""
    broker.subscribe(contract.PERFORMANCE_SUBSCRIPTION,
                     lambda topic, payload: handle_performance(broker, topic, payload))
    logger.info("Listening for %s", contract.PERFORMANCE_SUBSCRIPTION)
