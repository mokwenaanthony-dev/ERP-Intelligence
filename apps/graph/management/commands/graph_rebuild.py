"""``manage.py graph_rebuild [--clear]``: rebuild the whole graph from the ERP tables.

Safe to run at any time because projection is idempotent; use ``--clear`` after changing the ontology.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.graph import processor


class Command(BaseCommand):
    help = "Rebuild the Neo4j knowledge graph from the ERP tables (safe to run at any time)."

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="Delete everything in the graph first.")

    def handle(self, *args, clear=False, **options):
        if not settings.GRAPH_ENABLED:
            raise CommandError("The knowledge graph is off: set NEO4J_URI (Docker Compose does this).")
        count = processor.rebuild(clear=clear)
        self.stdout.write(self.style.SUCCESS(f"Projected {count} records into the knowledge graph."))
