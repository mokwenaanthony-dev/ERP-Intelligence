"""v0.23.1: a cell state remembers the publisher's session, so a late last will can be told apart."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0003_cell_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="cellstate",
            name="session",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The publisher's connection, if it says: tells a late last will of an earlier "
                          "connection from one of the current (v0.23.1).",
                max_length=64,
            ),
        ),
    ]
