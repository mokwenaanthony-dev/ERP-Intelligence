"""The UNS bridge against a real MQTT broker (Mosquitto). Skipped unless MQTT_URL is set.

CI starts ``eclipse-mosquitto`` and sets ``MQTT_URL``. Locally, start the broker from the
MES-Intelligence repository (or ``docker run -p 1883:1883 eclipse-mosquitto:2.0.20 mosquitto -c
/mosquitto-no-auth.conf``) and run::

    MQTT_URL=mqtt://localhost:1883 pytest tests/test_uns_mqtt.py --no-cov

Messages are received on paho's network thread and handed to the test through a queue; the database
work runs on the test's own thread, so the test transaction sees it.
"""
import json
import os
import queue
import uuid
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.masterdata.models import Material
from apps.organization.models import Plant
from apps.production import orders
from apps.uns import listener, publisher
from apps.uns.broker import MqttBroker

MQTT_URL = os.environ.get("MQTT_URL", "")
pytestmark = [pytest.mark.django_db,
              pytest.mark.skipif(not MQTT_URL, reason="MQTT_URL not set: no broker to test against")]


@pytest.fixture
def broker():
    connection = MqttBroker(MQTT_URL, client_id=f"erp-test-{uuid.uuid4().hex[:8]}")
    yield connection
    connection.close()


def collect(broker, pattern):
    inbox = queue.Queue()
    broker.subscribe(pattern, lambda topic, payload: inbox.put((topic, payload)))
    return inbox


def test_round_trip_retained_and_wildcards(broker):
    root = f"test-{uuid.uuid4().hex[:8]}"
    broker.publish(f"{root}/1000/assy/l1/press3/state/status", b'{"state": "RUNNING"}', retain=True)
    inbox = collect(broker, f"{root}/+/+/+/+/state/status")  # subscribed after: gets the retained state
    topic, payload = inbox.get(timeout=10)
    assert topic.endswith("press3/state/status") and json.loads(payload)["state"] == "RUNNING"
    broker.publish(f"{root}/1000/assy/l1/press3/state/status", b"", retain=True)  # clean up


def test_the_erp_publishes_and_answers_through_mosquitto(demo, settings, broker):
    settings.UNS_ENABLED, settings.GRAPH_ENABLED = True, False
    call_command("setup_demo_production", stdout=StringIO())
    order = orders.create_order(plant=Plant.objects.get(code="1000"), material=Material.objects.get(number="SF-3001"),
                                quantity=Decimal("100"), start_date=date(2026, 6, 1))
    orders.release_order(order, date(2026, 6, 1))
    publisher.publish_outbox(broker=broker)

    requests = collect(broker, "1000/1000/assy/l1/press3/schedule/operation-requests/+")
    topic, payload = requests.get(timeout=10)
    assert topic.endswith(f"{order.order_number.lower()}-0010")
    assert json.loads(payload)["data"]["planned_activities"] == {"LAB": "10.000", "MACH": "10.500"}

    results = collect(broker, "1000/1000/erp/events/confirmation-result")
    event = json.dumps({"schema": "operation-performance/v1", "event_id": f"it-{uuid.uuid4().hex[:12]}",
                        "data": {"order": order.order_number, "operation_number": 10, "posting_date": "2026-06-01",
                                 "yield_quantity": "100", "final": True}}).encode()
    listener.handle_performance(broker, "1000/1000/assy/l1/press3/events/operation-performance", event)
    _, answer = results.get(timeout=10)
    assert json.loads(answer)["data"]["status"] == "POSTED"

    orders.complete_order(order, date(2026, 6, 1))  # leave no retained request behind on the broker
    publisher.publish_outbox(broker=broker)
