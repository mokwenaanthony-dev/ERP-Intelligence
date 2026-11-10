"""Production orders (Wave 5b): order number on journal lines, new movement/document types.

The reporting views read this table, so they are dropped first and created again afterwards (rule
from Wave 4a: SQLite rebuilds the table to add a column, and PostgreSQL will not alter a column a
view depends on).
"""
from django.db import migrations, models

from apps.analytics.sql import create_views, drop_views


def _drop(apps, schema_editor):
    drop_views(schema_editor.connection)


def _create(apps, schema_editor):
    create_views(schema_editor.connection)


class Migration(migrations.Migration):

    dependencies = [
        ("analytics", "0002_create_views"),
        ("finance", "0005_cost_and_profit_centers"),
    ]

    operations = [
        migrations.RunPython(_drop, _create),
        migrations.AddField(
            model_name="journalline",
            name="order_number",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Production order this line is charged to (SAP AUFNR), a cost object like a cost centre.",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="journalentry",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("SA", "General journal"),
                    ("DR", "Customer invoice"),
                    ("DZ", "Customer payment"),
                    ("KR", "Supplier invoice"),
                    ("KZ", "Supplier payment"),
                    ("WE", "Goods receipt"),
                    ("WA", "Goods issue"),
                    ("WI", "Inventory adjustment"),
                    ("PY", "Payroll posting"),
                    ("PC", "Production confirmation (activity allocation)"),
                    ("PS", "Production order settlement"),
                ],
                max_length=2,
            ),
        ),
        migrations.RunPython(_create, _drop),
    ]
