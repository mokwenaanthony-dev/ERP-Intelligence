"""Connections to the MQTT broker: a real one (Mosquitto, via paho-mqtt) and an in-memory one.

Both have the same three methods, so the publisher and the listener do not care which they get
(the same idea as ``MemoryStore`` and ``Neo4jStore`` for the graph):

* ``publish(topic, payload, retain=False)``: send a message (QoS 1: delivered at least once);
* ``subscribe(pattern, callback)``: call ``callback(topic, payload)`` for every matching message;
* ``close()``.

``MemoryBroker`` behaves like a broker for everything the contract uses: retained messages (an
empty retained payload deletes the retained message), the ``+`` and ``#`` wildcards, and immediate
delivery of retained messages to a new subscriber. The tests run against it; CI also runs a real
Mosquitto for the integration test.
"""
import logging
from urllib.parse import unquote, urlparse

from django.conf import settings

logger = logging.getLogger(__name__)


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard matching: ``+`` is one level, ``#`` (last) is any number of levels."""
    pattern_levels, topic_levels = pattern.split("/"), topic.split("/")
    for index, level in enumerate(pattern_levels):
        if level == "#":
            return True
        if index >= len(topic_levels) or (level != "+" and level != topic_levels[index]):
            return False
    return len(pattern_levels) == len(topic_levels)


class MemoryBroker:
    """An in-process broker for tests and dry runs."""

    def __init__(self):
        self.retained = {}       # topic -> payload
        self.published = []      # every (topic, payload, retain) in order, for assertions
        self.subscriptions = []  # (pattern, callback)

    def publish(self, topic, payload, retain=False):
        """Store a retained payload (an empty one deletes it) and deliver to matching subscribers."""
        payload = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.published.append((topic, payload, retain))
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        for pattern, callback in list(self.subscriptions):
            if topic_matches(pattern, topic):
                callback(topic, payload)

    def subscribe(self, pattern, callback):
        """Register ``callback`` and send it the retained messages that match, as a broker does."""
        self.subscriptions.append((pattern, callback))
        for topic, payload in list(self.retained.items()):
            if topic_matches(pattern, topic):
                callback(topic, payload)

    def close(self):
        """Nothing to close."""


class MqttBroker:  # exercised by the integration test in CI, which runs Mosquitto
    """A real MQTT connection (paho-mqtt 2.x, MQTT 3.1.1, QoS 1).

    ``persistent=True`` keeps the session on the broker between connections (fixed client id, no
    clean session), so QoS 1 messages sent while the ERP was down are delivered when it reconnects:
    the ERP's half of store-and-forward.
    """

    def __init__(self, url: str, client_id: str, persistent: bool = False):
        from paho.mqtt import client as mqtt  # imported here: only needed when a broker is configured

        self._mqtt = mqtt
        parts = urlparse(url)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                                  clean_session=not persistent)
        if parts.username:
            self.client.username_pw_set(unquote(parts.username), unquote(parts.password or ""))
        if parts.scheme == "mqtts":
            self.client.tls_set()
        self._subscriptions = []
        self.client.on_connect = self._on_connect
        self.client.connect(parts.hostname, parts.port or (8883 if parts.scheme == "mqtts" else 1883), keepalive=60)
        self.client.loop_start()
        logger.info("Connected to MQTT broker %s:%s as %s", parts.hostname, parts.port or 1883, client_id)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        # Subscribe again after every (re)connection.
        for pattern in self._subscriptions:
            client.subscribe(pattern, qos=1)

    def publish(self, topic, payload, retain=False):
        """Publish with QoS 1 and wait until the broker has it; raises if it cannot be sent."""
        info = self.client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=10)
        if info.rc != self._mqtt.MQTT_ERR_SUCCESS or not info.is_published():
            raise ConnectionError(f"MQTT publish to {topic} failed (rc={info.rc}).")

    def subscribe(self, pattern, callback):
        """Deliver every message matching ``pattern`` to ``callback(topic, payload)`` (QoS 1)."""
        self._subscriptions.append(pattern)
        self.client.message_callback_add(pattern, lambda client, userdata, msg: callback(msg.topic, msg.payload))
        self.client.subscribe(pattern, qos=1)

    def close(self):
        """Stop the network loop and disconnect."""
        self.client.loop_stop()
        self.client.disconnect()


_publisher = None


def publisher_broker():
    """The process-wide publishing connection, opened on first use (None when the UNS is off)."""
    global _publisher
    if not settings.UNS_ENABLED:
        return None
    if _publisher is None:
        import os

        _publisher = MqttBroker(settings.MQTT_URL, client_id=f"erp-intelligence-publisher-{os.getpid()}")
    return _publisher
