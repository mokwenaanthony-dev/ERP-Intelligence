"""``manage.py graph_process``: project pending outbox events into Neo4j now (what Celery beat does every 10 s).
"""
from django.core.management.base import BaseCommand

from apps.graph import processor


class Command(BaseCommand):
    help = "Project pending outbox events into the graph now (Celery beat does this every 10 seconds)."

    def handle(self, *args, **options):
        result = processor.process_outbox()
        if not result["enabled"]:
            self.stdout.write("The knowledge graph is off (NEO4J_URI is not set).")
            return
        self.stdout.write(self.style.SUCCESS(f"Processed {result['processed']} events, {result['failed']} failed."))
        if result.get("error"):
            self.stdout.write(self.style.ERROR(f"Neo4j error: {result['error']}"))
