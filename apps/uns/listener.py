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

Wave 6d: it also subscribes to ``+/+/+/+/+/state/status``, the retained cell state that edge agents
and the MES publish (``equipment-state/v1``). ``handle_state`` keeps the latest state per work centre
(``apps/production/shopfloor.py``), from where it reaches the graph's ``Cell`` nodes. State is not an
event, so it gets no answer.
"""
import json
import logging
from datetime import date, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import close_old_connections
from django.utils.dateparse import parse_datetime

from apps.common.logcontext import request_id
from apps.finance.services import PostingError
from apps.production import orders, shopfloor
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


def _when(value, name):
    """An ISO 8601 date-time from a message; one without an offset is taken as UTC."""
    moment = parse_datetime(str(value)) if value else None
    if moment is None:
        raise MessageError(f"{name} must be an ISO 8601 date-time, got {value!r}.")
    return moment if moment.tzinfo else moment.replace(tzinfo=dt_timezone.utc)


def handle_state(topic: str, payload: bytes) -> str:
    """Keep one cell's retained state (``equipment-state/v1``). Returns what happened, for logs and
    tests: ``created``, ``updated``, ``unchanged``, ``stale``, ``cleared`` or ``ignored``.
    """
    levels = topic.split("/")
    if len(levels) != 7 or levels[5:] != ["state", "status"]:
        logger.warning("Ignored message on unexpected topic %s", topic)
        return "ignored"
    path = "/".join(levels[:5])
    work_center = shopfloor.work_center_for_path(path)
    if work_center is None:
        logger.info("State for %s ignored: no such work centre in the ERP", path)
        return "ignored"
    if not payload:  # the retained state was cleared: the ERP no longer knows the cell's state
        return "cleared" if shopfloor.clear_cell_state(work_center) else "unchanged"
    try:
        message = _parse(topic, payload)
        if message.get("schema") != contract.EQUIPMENT_STATE:
            logger.warning("Ignored message with unknown schema %r on %s", message.get("schema"), topic)
            return "ignored"
        data = message["data"]
        state = data.get("state")
        if state not in contract.STATES:
            raise MessageError(f"state must be one of {', '.join(contract.STATES)}, got {state!r}.")
        operation = data.get("operation_number")
        if operation is not None and (not isinstance(operation, int) or isinstance(operation, bool)):
            raise MessageError(f"operation_number must be a whole number, got {operation!r}.")
        report = shopfloor.Report(
            state=state,
            since=_when(data["since"], "since") if data.get("since") else None,
            order_number=str(data.get("order") or "")[:20],
            operation_number=operation,
            source=str(message.get("source") or "")[:64],
            reported_at=_when(message.get("timestamp"), "timestamp"),
        )
    except MessageError as exc:
        logger.warning("State message on %s ignored: %s", topic, exc)
        return "ignored"
    return shopfloor.record_cell_state(work_center, report)


def listen(broker, manage_db_connections: bool = False) -> None:
    """Subscribe to every cell's performance events and states; the caller keeps the process alive.

    ``manage_db_connections`` is for the long-running ``uns_listen`` process, where messages arrive
    on paho's network thread: like Django does around each web request, a stale database connection
    is dropped before each message and an expired one after it. Never inside a test or a request:
    there the caller's transaction owns the connection, and closing it would break that transaction
    (found in CI, where PostgreSQL closes it; SQLite's in-memory database never does).
    """
    def housekeeping(handler):
        """Wrap a handler with Django's connection housekeeping when running as the listener process."""
        def on_message(topic, payload):
            """Handle one message; a failure is logged and never stops the listener."""
            if manage_db_connections:
                close_old_connections()
            try:
                handler(topic, payload)
            except Exception:
                logger.exception("Could not handle message on %s", topic)
            finally:
                if manage_db_connections:
                    close_old_connections()
        return on_message

    broker.subscribe(contract.PERFORMANCE_SUBSCRIPTION,
                     housekeeping(lambda topic, payload: handle_performance(broker, topic, payload)))
    broker.subscribe(contract.STATE_SUBSCRIPTION, housekeeping(handle_state))
    logger.info("Listening for %s and %s", contract.PERFORMANCE_SUBSCRIPTION, contract.STATE_SUBSCRIPTION)
