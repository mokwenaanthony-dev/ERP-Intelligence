"""Retire the Wave 0 sales models (Customer, SalesOrder, SalesOrderLine).

Wave 2b rebuilds order-to-cash on Business Partner and Material. The old tables are
dropped here, and the new models are created by the next migration. The Wave 0 code
remains available in the Git history and in release v0.1.0.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="SalesOrderLine"),
        migrations.DeleteModel(name="SalesOrder"),
        migrations.DeleteModel(name="Customer"),
    ]
