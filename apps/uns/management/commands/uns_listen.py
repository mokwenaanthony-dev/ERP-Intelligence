"""``manage.py uns_listen``: receive operation-performance events from the MES and post them.

A long-running process (one per deployment; the Compose service ``uns_listener``). It keeps a
persistent MQTT session, so events sent while it was stopped are delivered when it starts again.
"""
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.uns.broker import MqttBroker
from apps.uns.listener import listen


class Command(BaseCommand):
    help = "Listen for MES operation-performance events and post them as confirmations (needs MQTT_URL)."

    def handle(self, *args, **options):
        if not settings.UNS_ENABLED:
            raise CommandError("The UNS bridge is off: set MQTT_URL (e.g. mqtt://localhost:1883).")
        broker = MqttBroker(settings.MQTT_URL, client_id="erp-intelligence-listener", persistent=True)
        listen(broker, manage_db_connections=True)
        self.stdout.write(self.style.SUCCESS("Listening for operation-performance events. Ctrl+C to stop."))
        try:
            threading.Event().wait()  # the MQTT client's own thread does the work
        except KeyboardInterrupt:
            self.stdout.write("Stopping.")
        finally:
            broker.close()
