"""Live cell state from the shop floor (Wave 6d, ADR 0011).

Edge agents and the MES publish each cell's state as a retained ``equipment-state/v1`` message on
``<cell>/state/status``. The UNS listener keeps the latest one per work centre (``CellState``) and the
outbox projects it onto the graph's ``Cell`` node. Every step is repeated or reordered to prove it is
idempotent and order-safe (ADR 0010). The message format is exactly what MES-Intelligence's Rust edge
agent writes (``edge/src/messages.rs``: millisecond timestamps, ``+00:00`` offsets).
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Role
from apps.control import services as control
from apps.graph import processor
from apps.graph.models import OutboxEvent
from apps.graph.store import MemoryStore
from apps.masterdata.models import Material
from apps.organization.models import Plant
from apps.production import orders
from apps.production.models import CellState, WorkCenter
from apps.uns import contract, listener
from apps.uns.broker import MemoryBroker

pytestmark = pytest.mark.django_db
PRESS = "1000/1000/assy/l1/press3"
STATE_TOPIC = f"{PRESS}/state/status"


@pytest.fixture
def shop(demo, settings):
    """UNS and graph both on, demo production data, the listener subscribed to a memory broker."""
    settings.UNS_ENABLED, settings.GRAPH_ENABLED = True, True
    call_command("setup_demo_production", stdout=StringIO())
    demo.broker = MemoryBroker()
    listener.listen(demo.broker)
    demo.press = WorkCenter.objects.get(code="PRESS3")
    order = orders.create_order(plant=Plant.objects.get(code="1000"),
                                material=Material.objects.get(number="SF-3001"),
                                quantity=Decimal("100"), start_date=date(2026, 6, 1))
    demo.order = orders.release_order(order, date(2026, 6, 1))
    demo.store = MemoryStore()
    processor.process_outbox(limit=100000, store=demo.store)  # the graph as it was before any state
    return demo


def state(value="RUNNING", order="MO00000001", operation=10, since="2026-06-01T08:00:00.000+00:00",
          at="2026-06-01T08:00:00.120+00:00", source="mes-edge/press3", schema="equipment-state/v1", session=None):
    """An equipment-state message as the Rust edge agent writes it (``session`` only when given)."""
    data = {"state": value, "order": order, "operation_number": operation, "since": since}
    if session is not None:
        data["session"] = session
    return json.dumps({"schema": schema, "event_id": None, "timestamp": at, "source": source, "data": data}).encode()


def state_events():
    return OutboxEvent.objects.filter(aggregate_type="production.cellstate").count()


def test_a_running_cell_is_kept_and_reaches_the_graph(shop):
    shop.broker.publish(STATE_TOPIC, state(), retain=True)
    live = CellState.objects.get(work_center=shop.press)
    assert (live.state, live.order_number, live.operation_number, live.source) == (
        "RUNNING", "MO00000001", 10, "mes-edge/press3")
    assert live.since == datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    assert state_events() == 1

    processor.process_outbox(limit=100000, store=shop.store)
    cell = shop.store.props("Cell", "1000/PRESS3")
    assert (cell["state"], cell["state_order"], cell["state_operation"]) == ("RUNNING", "MO00000001", 10)
    assert cell["code"] == "PRESS3"  # the rest of the cell is still there
    [(label, key, props)] = shop.store.related("Cell", "1000/PRESS3", "RUNNING")
    assert (label, key, props["operation_number"]) == ("ProductionOrder", "1000/MO00000001", 10)


def test_the_same_state_again_writes_nothing(shop):
    for _ in range(3):  # the retained message on every reconnect, or a QoS 1 redelivery
        shop.broker.publish(STATE_TOPIC, state(at="2026-06-01T08:05:00.000+00:00"), retain=True)
    assert state_events() == 1
    assert CellState.objects.count() == 1


def test_an_older_report_never_overwrites_a_newer_one(shop):
    shop.broker.publish(STATE_TOPIC, state("IDLE", order=None, operation=None, since="2026-06-01T18:30:00+00:00",
                                           at="2026-06-01T18:30:00.000+00:00"), retain=True)
    late = state(at="2026-06-01T08:00:00.120+00:00")  # the morning's RUNNING, delivered late
    assert listener.handle_state(STATE_TOPIC, late) == "stale"
    live = CellState.objects.get(work_center=shop.press)
    assert (live.state, live.order_number, live.operation_number) == ("IDLE", "", None)


# --- last wills (v0.23.1) ---------------------------------------------------------------------------
# The Rust edge agent registers its OFFLINE will when it connects, so the broker publishes it, when the
# agent dies, with the connection's timestamp: older than everything the agent said since. Version
# 0.23.0 ignored it as an older report and kept showing a dead cell as RUNNING, in the equipment tree,
# on the graph and (by not raising it) in the control tower.

def will(at="2026-06-01T07:59:58.000+00:00", source="mes-edge/press3", session=None):
    """The OFFLINE last will exactly as the edge agent builds it: stamped when it connected."""
    return state("OFFLINE", order=None, operation=None, since=at, at=at, source=source, session=session)


def running_at_ten(shop, session=None):
    """The press reported RUNNING at 10:00, two hours after its agent connected."""
    shop.broker.publish(STATE_TOPIC, state(at="2026-06-01T10:00:00.000+00:00", session=session), retain=True)


def test_a_last_will_takes_a_dead_cell_offline_whatever_its_timestamp(shop):
    running_at_ten(shop)
    events = state_events()
    before = datetime.now(timezone.utc)
    assert listener.handle_state(STATE_TOPIC, will()) == "updated"

    live = CellState.objects.get(work_center=shop.press)
    assert (live.state, live.order_number, live.operation_number) == ("OFFLINE", "", None)
    assert live.since >= before  # when the ERP heard it: the will cannot know when it was sent
    assert live.reported_at == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)  # time never goes back
    assert state_events() == events + 1

    processor.process_outbox(limit=100000, store=shop.store)
    assert shop.store.props("Cell", "1000/PRESS3")["state"] == "OFFLINE"
    assert shop.store.related("Cell", "1000/PRESS3", "RUNNING") == []

    found = control.problems(shop.company, on=date.today())
    assert any(p["key"].startswith("cell:1000/PRESS3:OFFLINE:") for p in found)


def test_the_same_will_again_changes_nothing(shop):
    running_at_ten(shop)
    shop.broker.publish(STATE_TOPIC, will(), retain=True)
    events, since = state_events(), CellState.objects.get(work_center=shop.press).since
    for _ in range(3):  # retained: every reconnect of the listener delivers it again
        assert listener.handle_state(STATE_TOPIC, will()) == "unchanged"
    assert state_events() == events
    assert CellState.objects.get(work_center=shop.press).since == since


def test_an_old_offline_from_another_source_is_still_stale(shop):
    running_at_ten(shop)
    assert listener.handle_state(STATE_TOPIC, will(source="mes-intelligence")) == "stale"
    assert CellState.objects.get(work_center=shop.press).state == "RUNNING"


def test_a_will_of_an_earlier_session_is_ignored(shop):
    running_at_ten(shop, session="press3-s2")  # the agent reconnected: session 2
    assert listener.handle_state(STATE_TOPIC, will(session="press3-s1")) == "stale"
    assert CellState.objects.get(work_center=shop.press).state == "RUNNING"
    assert listener.handle_state(STATE_TOPIC, will(session="press3-s2")) == "updated"  # its own will
    live = CellState.objects.get(work_center=shop.press)
    assert (live.state, live.session) == ("OFFLINE", "press3-s2")


def test_after_a_will_newer_reports_win_and_older_ones_stay_stale(shop):
    running_at_ten(shop)
    listener.handle_state(STATE_TOPIC, will())
    late = state(at="2026-06-01T09:00:00.000+00:00")  # a delayed report from before it died
    assert listener.handle_state(STATE_TOPIC, late) == "stale"
    back = state(since="2026-06-01T11:00:00.000+00:00", at="2026-06-01T11:00:00.000+00:00")  # it came back
    assert listener.handle_state(STATE_TOPIC, back) == "updated"
    assert CellState.objects.get(work_center=shop.press).state == "RUNNING"


def test_idle_after_running_removes_the_running_order_from_the_graph(shop):
    shop.broker.publish(STATE_TOPIC, state(), retain=True)
    processor.process_outbox(limit=100000, store=shop.store)
    shop.broker.publish(STATE_TOPIC, state("IDLE", order=None, operation=None, since="2026-06-01T18:30:00+00:00",
                                           at="2026-06-01T18:30:00.000+00:00"), retain=True)
    processor.process_outbox(limit=100000, store=shop.store)
    cell = shop.store.props("Cell", "1000/PRESS3")
    assert cell["state"] == "IDLE" and "state_order" not in cell and "state_operation" not in cell
    assert shop.store.related("Cell", "1000/PRESS3", "RUNNING") == []


def test_a_cleared_state_is_forgotten(shop):
    shop.broker.publish(STATE_TOPIC, state("DOWN", order=None, operation=None), retain=True)
    shop.broker.publish(STATE_TOPIC, b"", retain=True)  # the cell left the namespace
    assert not CellState.objects.exists()
    processor.process_outbox(limit=100000, store=shop.store)
    assert "state" not in shop.store.props("Cell", "1000/PRESS3")
    assert listener.handle_state(STATE_TOPIC, b"") == "unchanged"  # clearing again changes nothing


def test_running_an_unknown_order_adds_no_order_node(shop):
    shop.broker.publish(STATE_TOPIC, state(order="MO99999999"), retain=True)
    processor.process_outbox(limit=100000, store=shop.store)
    assert shop.store.props("Cell", "1000/PRESS3")["state_order"] == "MO99999999"
    assert shop.store.related("Cell", "1000/PRESS3", "RUNNING") == []
    assert ("ProductionOrder", "1000/MO99999999") not in shop.store.nodes


@pytest.mark.parametrize("topic, payload", [
    ("1000/1000/assy/l1/nosuch/state/status", state()),                     # not a work centre of the ERP
    (STATE_TOPIC, state("SMOKING")),                                         # not a contract state
    (STATE_TOPIC, state(schema="equipment-state/v9")),                       # a schema we do not know
    (STATE_TOPIC, b"not json"),
    (STATE_TOPIC, state(since="yesterday")),
    (STATE_TOPIC, state(at="")),
    (STATE_TOPIC, state(operation=True)),
    (STATE_TOPIC, state(operation="10")),
    ("1000/1000/assy/l1/press3/state", state()),                             # not a state topic
])
def test_bad_or_foreign_state_messages_are_ignored(shop, topic, payload):
    assert listener.handle_state(topic, payload) == "ignored"
    assert not CellState.objects.exists()


def test_a_failing_message_never_stops_the_listener(shop, monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(listener.shopfloor, "record_cell_state", explode)
    shop.broker.publish(STATE_TOPIC, state(), retain=True)  # logged, not raised into the MQTT thread
    assert "Could not handle message" in caplog.text
    monkeypatch.undo()
    shop.broker.publish(STATE_TOPIC, state(at="2026-06-01T08:00:01.000+00:00"), retain=True)
    assert CellState.objects.get(work_center=shop.press).state == "RUNNING"


def test_the_equipment_tree_shows_live_state(shop, client_for):
    shop.broker.publish(STATE_TOPIC, state(), retain=True)
    tree = client_for(Role.VIEWER).get("/api/production/equipment/?plant=1000").json()
    cells = {c["code"]: c for a in tree["areas"] for line in a["lines"] for c in line["work_centers"]}
    assert cells["PRESS3"]["live_state"] == {
        "state": "RUNNING", "since": "2026-06-01T08:00:00Z", "order": "MO00000001", "operation_number": 10,
        "source": "mes-edge/press3", "session": None, "reported_at": "2026-06-01T08:00:00.120000Z"}
    assert cells["ASSY1"]["live_state"] is None  # no report yet: the ERP does not guess


def test_the_listener_subscribes_to_cell_states(shop):
    assert (contract.STATE_SUBSCRIPTION, ) == tuple(p for p, _ in shop.broker.subscriptions
                                                    if p == contract.STATE_SUBSCRIPTION)
    assert contract.STATE_SUBSCRIPTION == "+/+/+/+/+/state/status"


def test_a_rebuilt_graph_has_the_same_state(shop):
    shop.broker.publish(STATE_TOPIC, state("DOWN", order=None, operation=None), retain=True)
    rebuilt = MemoryStore()
    processor.rebuild(store=rebuilt)
    assert rebuilt.props("Cell", "1000/PRESS3")["state"] == "DOWN"  # the graph is still a read model
