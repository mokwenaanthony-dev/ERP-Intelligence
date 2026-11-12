"""The ERP's side of the Unified Namespace (Wave 6), against the contract in ``docs/UNS_CONTRACT.md``.

Runs against ``MemoryBroker``, which keeps retained messages and matches wildcards like Mosquitto.
The scenario: frame order MO00000001 (100 x SF-3001) is released; its operation 10 appears at the
press, ``1000/1000/assy/l1/press3``; the MES reports the work; the ERP posts the confirmation and
answers. Every step is repeated to prove it is idempotent (ADR 0010).
"""
import json
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.graph.models import OutboxEvent
from apps.masterdata.models import Material
from apps.organization.models import Plant
from apps.production import orders
from apps.production.models import Confirmation, WorkCenter
from apps.uns import contract, listener, publisher
from apps.uns.broker import MemoryBroker, publisher_broker, topic_matches
from apps.uns.models import OutboxCursor

pytestmark = pytest.mark.django_db
D = Decimal
PRESS = "1000/1000/assy/l1/press3"
PERFORMANCE = f"{PRESS}/events/operation-performance"
RESULTS = "1000/1000/erp/events/confirmation-result"


@pytest.fixture
def uns(demo, settings):
    """UNS on, graph off (so only this module's consumer matters), demo production data loaded."""
    settings.UNS_ENABLED, settings.GRAPH_ENABLED = True, False
    call_command("setup_demo_production", stdout=StringIO())
    demo.broker = MemoryBroker()
    demo.plant = Plant.objects.get(code="1000")
    return demo


def released_frame_order(uns, quantity="100"):
    order = orders.create_order(plant=uns.plant, material=Material.objects.get(number="SF-3001"),
                                quantity=D(quantity), start_date=date(2026, 6, 1))
    return orders.release_order(order, date(2026, 6, 1))


def retained(broker, topic):
    payload = broker.retained.get(topic)
    return json.loads(payload) if payload else None


def performance(event_id="press3-evt-0001", order="MO00000001", operation=10, **data):
    body = {"order": order, "operation_number": operation, "posting_date": "2026-06-01",
            "yield_quantity": "100", "scrap_quantity": "0", "activities": {"MACH": "10.5", "LAB": "10"},
            "final": True, **data}
    return json.dumps({"schema": "operation-performance/v1", "event_id": event_id,
                       "timestamp": "2026-06-01T10:15:00+02:00", "source": "mes-intelligence", "data": body}).encode()


def results(broker):
    return [json.loads(p)["data"] for t, p, _ in broker.published if t == RESULTS]


# --- broker ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("pattern, topic, expected", [
    ("+/+/+/+/+/events/operation-performance", PERFORMANCE, True),
    ("+/+/+/+/+/events/operation-performance", "1000/1000/erp/events/confirmation-result", False),
    ("1000/#", PERFORMANCE, True),
    ("1000/1000/+", "1000/1000/assy/l1", False),
    ("a/b", "a/b", True),
    ("a/b/c", "a/b", False),
])
def test_wildcards(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


def test_memory_broker_keeps_and_clears_retained_messages():
    broker, seen = MemoryBroker(), []
    broker.publish("a/b/state", "RUNNING", retain=True)
    broker.subscribe("a/+/state", lambda t, p: seen.append(p))  # a new subscriber gets the retained state
    broker.publish("a/b/state", b"", retain=True)                # an empty retained payload deletes it
    assert seen == [b"RUNNING", b""] and broker.retained == {}
    broker.close()


# --- publishing from the outbox --------------------------------------------------------------------

def test_equipment_and_operation_requests_are_published(uns):
    order = released_frame_order(uns)
    result = publisher.publish_outbox(broker=uns.broker)
    assert result["enabled"] and result["events"] > 0

    equipment = retained(uns.broker, f"{PRESS}/definition/equipment")
    assert equipment["schema"] == "equipment/v1" and equipment["source"] == "erp-intelligence"
    assert equipment["data"] == {
        "company_code": "1000", "plant": "1000", "area": "ASSY", "line": "L1", "work_center": "PRESS3",
        "name": "Press 3", "isa95_level": "WorkCell", "cost_center": "4000", "activity_types": ["LAB", "MACH"],
        "is_active": True, "uns_path": PRESS,
    }
    request = retained(uns.broker, f"{PRESS}/schedule/operation-requests/mo00000001-0010")
    assert request["schema"] == "operation-request/v1" and request["event_id"].startswith("erp-outbox-")
    assert request["data"] == {
        "order": order.order_number, "operation_number": 10, "description": "Press frame", "material": "SF-3001",
        "material_description": "Pressed frame", "quantity": "100.000", "unit": "EA", "start_date": "2026-06-01",
        "finish_date": "2026-06-01", "status": "RELEASED",
        "planned_activities": {"LAB": "10.000", "MACH": "10.500"},  # exact decimal strings (ADR 0008)
    }


def test_a_completed_order_clears_its_requests(uns):
    order = released_frame_order(uns, "10")
    publisher.publish_outbox(broker=uns.broker)
    topic = f"{PRESS}/schedule/operation-requests/mo00000001-0010"
    assert topic in uns.broker.retained
    orders.complete_order(order, date(2026, 6, 1))
    publisher.publish_outbox(broker=uns.broker)
    assert topic not in uns.broker.retained


def test_a_created_order_is_not_yet_shop_floor_work(uns):
    orders.create_order(plant=uns.plant, material=Material.objects.get(number="SF-3001"), quantity=D("5"),
                        start_date=date(2026, 6, 1))
    publisher.publish_outbox(broker=uns.broker)
    assert not [t for t in uns.broker.retained if "/schedule/" in t]


def test_publishing_is_idempotent(uns):
    released_frame_order(uns)
    publisher.publish_outbox(broker=uns.broker)
    state = dict(uns.broker.retained)
    assert publisher.publish_outbox(broker=uns.broker)["events"] == 0  # nothing new: the cursor moved on
    assert publisher.republish(broker=uns.broker) == 3  # two cells and one operation request
    assert uns.broker.retained.keys() == state.keys()
    assert all(json.loads(uns.broker.retained[t])["data"] == json.loads(state[t])["data"] for t in state)


def test_a_broker_failure_keeps_the_events_for_the_next_run(uns):
    class Down(MemoryBroker):
        def publish(self, topic, payload, retain=False):
            raise ConnectionError("broker unreachable")

    released_frame_order(uns)
    down = publisher.publish_outbox(broker=Down())
    # Events that need no message (areas, lines) are passed; the cursor stops at the first that failed.
    first_unsent = OutboxEvent.objects.filter(aggregate_type="production.workcenter").order_by("pk").first()
    assert down["cursor"] == OutboxCursor.objects.get(name="uns").last_event_id < first_unsent.pk
    back = publisher.publish_outbox(broker=uns.broker)  # broker back: everything is published
    assert back["cursor"] == OutboxEvent.objects.order_by("-pk").first().pk
    # Both order events (created, released) read the order's current state, so the request is published
    # twice: the same retained message, which is harmless by design (state-based, ADR 0010).
    assert len(uns.broker.retained) == 3 and back["messages"] == 4


def test_a_deleted_cell_is_cleared(uns):
    cell = WorkCenter.objects.create(plant=uns.plant, line=WorkCenter.objects.get(code="PRESS3").line,
                                     code="SPARE1", name="Spare", cost_center=WorkCenter.objects.get(code="PRESS3")
                                     .cost_center)
    publisher.publish_outbox(broker=uns.broker)
    topic = "1000/1000/assy/l1/spare1/definition/equipment"
    assert topic in uns.broker.retained
    cell.delete()
    publisher.publish_outbox(broker=uns.broker)
    assert topic not in uns.broker.retained


def test_the_outbox_is_written_when_only_the_uns_is_on(uns):
    before = OutboxEvent.objects.count()
    released_frame_order(uns)
    assert OutboxEvent.objects.count() > before


def test_off_without_mqtt_url(settings):
    settings.UNS_ENABLED = False
    assert publisher.publish_outbox() == {"enabled": False}
    assert publisher_broker() is None
    for command in ("uns_listen", "uns_republish"):
        with pytest.raises(CommandError, match="MQTT_URL"):
            call_command(command, stdout=StringIO())


# --- receiving performance -------------------------------------------------------------------------

def test_performance_becomes_a_confirmation_and_is_answered(uns):
    order = released_frame_order(uns)
    listener.listen(uns.broker)
    uns.broker.publish(PERFORMANCE, performance())
    [result] = results(uns.broker)
    confirmation = Confirmation.objects.get(order=order)
    assert result == {"event_id": "press3-evt-0001", "status": "POSTED", "order": order.order_number,
                      "confirmation_number": "0001", "journal_entry": confirmation.journal_entry.document_number,
                      "reason": None, "replayed": False}
    assert confirmation.external_id == "press3-evt-0001"
    assert {a.activity_type.code: a.amount for a in confirmation.activities.all()} == {
        "LAB": D("2200.00"), "MACH": D("4725.00")}


def test_a_repeated_event_gets_the_same_answer_and_posts_once(uns):
    released_frame_order(uns)
    listener.listen(uns.broker)
    uns.broker.publish(PERFORMANCE, performance())
    uns.broker.publish(PERFORMANCE, performance())  # MQTT QoS 1 may deliver twice
    first, second = results(uns.broker)
    assert second == {**first, "replayed": True}
    assert Confirmation.objects.count() == 1


@pytest.mark.parametrize("payload, reason", [
    (performance(order="MO99999999"), "Order MO99999999 not found"),
    (performance(operation=99), "has no operation 99"),
    (performance(activities={"ENGY": "1"}), "not recorded at work centre PRESS3"),
    (performance(yield_quantity="lots"), "decimal string"),
    (performance(posting_date="June"), "Missing or invalid field"),
    (performance(activities=["MACH"]), "must be an object"),
    (performance(event_id=""), "event_id is required"),
    (b"not json", "not JSON"),
    (b'{"schema": "operation-performance/v1"}', "not a contract envelope"),
])
def test_invalid_events_are_rejected_with_a_reason(uns, payload, reason):
    released_frame_order(uns)
    result = listener.handle_performance(uns.broker, PERFORMANCE, payload)
    assert result["status"] == "REJECTED" and reason in result["reason"]
    assert results(uns.broker) == [result] and not Confirmation.objects.exists()


def test_work_reported_at_the_wrong_cell_is_rejected(uns):
    released_frame_order(uns)
    result = listener.handle_performance(uns.broker, "1000/1000/assy/l1/assy1/events/operation-performance",
                                         performance())
    assert result["status"] == "REJECTED" and f"runs at {PRESS}" in result["reason"]


def test_an_order_that_is_not_released_is_rejected(uns):
    order = released_frame_order(uns, "10")
    orders.complete_order(order, date(2026, 6, 1))
    result = listener.handle_performance(uns.broker, PERFORMANCE, performance())
    assert result["status"] == "REJECTED" and "only released orders" in result["reason"]


def test_messages_that_are_not_ours_are_ignored(uns):
    assert listener.handle_performance(uns.broker, "1000/1000/assy/l1/press3/state/status", b"{}") is None
    other = json.dumps({"schema": "operation-performance/v9", "event_id": "x" * 10, "data": {}}).encode()
    assert listener.handle_performance(uns.broker, PERFORMANCE, other) is None
    assert results(uns.broker) == []


def test_contract_helpers():
    assert contract.operation_request_topic(PRESS, "MO00000001", 10) == \
        f"{PRESS}/schedule/operation-requests/mo00000001-0010"
    assert contract.confirmation_result_topic("1000", "1000") == RESULTS
    message = json.loads(contract.envelope("x/v1", {"amount": D("1.50")}, "e-1"))
    assert message["data"] == {"amount": "1.50"} and message["event_id"] == "e-1"
