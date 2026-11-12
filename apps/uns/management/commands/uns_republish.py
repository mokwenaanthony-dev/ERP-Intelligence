"""``manage.py uns_republish``: publish the current state of every work centre and released order.

For a new or emptied broker, or after the bridge was off. Safe at any time: retained state published
again changes nothing (ADR 0010).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.uns.publisher import republish


class Command(BaseCommand):
    help = "Publish all equipment definitions and open operation requests to the UNS (needs MQTT_URL)."

    def handle(self, *args, **options):
        if not settings.UNS_ENABLED:
            raise CommandError("The UNS bridge is off: set MQTT_URL (e.g. mqtt://localhost:1883).")
        count = republish()
        self.stdout.write(self.style.SUCCESS(f"Published {count} retained message(s)."))
