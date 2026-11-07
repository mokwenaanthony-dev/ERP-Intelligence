from django.core.management.base import BaseCommand

from apps.analytics import services


class Command(BaseCommand):
    help = "Refresh the analytics materialized views (PostgreSQL). On SQLite the views are live."

    def handle(self, *args, **options):
        results = services.refresh_views()
        if not results:
            self.stdout.write("Not PostgreSQL: the analytics views are ordinary views and always current.")
        for row in results:
            self.stdout.write(self.style.SUCCESS(f"Refreshed {row['view']} in {row['duration_ms']} ms"))
