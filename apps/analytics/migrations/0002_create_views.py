"""Create the reporting views: materialized on PostgreSQL, ordinary views on SQLite (see sql.py)."""
from django.db import migrations

from apps.analytics.sql import create_views, drop_views


def forwards(apps, schema_editor):
    create_views(schema_editor.connection)


def backwards(apps, schema_editor):
    drop_views(schema_editor.connection)


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0001_initial"),
        ("finance", "0005_cost_and_profit_centers"),
        ("inventory", "0003_delete_product"),
        ("organization", "0001_initial"),
        ("hr", "0001_initial"),
        ("controlling", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
