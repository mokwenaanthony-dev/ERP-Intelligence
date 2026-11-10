"""Production orders (Wave 5b): order number on material documents, new movement/document types.

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
        ("inventory", "0003_delete_product"),
    ]

    operations = [
        migrations.RunPython(_drop, _create),
        migrations.AddField(
            model_name="materialdocument",
            name="order_number",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Production order issued to (261) or received from (101).",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryaccountsetting",
            name="account_key",
            field=models.CharField(
                choices=[
                    ("INVENTORY", "Inventory (BSX)"),
                    ("GR_IR", "Goods received / invoice received clearing (WRX)"),
                    ("INITIAL_ENTRY", "Initial stock entry offset"),
                    ("COST_OF_SALES", "Cost of goods sold (GBB-VAX)"),
                    ("SCRAP", "Scrapping expense (GBB-VNG)"),
                    ("DIFFERENCES", "Physical inventory differences (GBB-INV)"),
                    ("CONSUMPTION", "Consumption for production orders (GBB-VBR)"),
                    ("FACTORY_OUTPUT", "Factory output credited to production orders"),
                    ("PROD_VARIANCE", "Production order variances (settlement)"),
                ],
                max_length=15,
            ),
        ),
        migrations.AlterField(
            model_name="materialdocument",
            name="movement_type",
            field=models.CharField(
                choices=[
                    ("101", "Goods receipt"),
                    ("561", "Initial entry of stock"),
                    ("601", "Goods issue for delivery"),
                    ("551", "Scrapping"),
                    ("701", "Physical inventory gain"),
                    ("702", "Physical inventory loss"),
                    ("311", "Transfer between storage locations"),
                    ("261", "Goods issue to production order"),
                ],
                max_length=3,
            ),
        ),
        migrations.RunPython(_create, _drop),
    ]
