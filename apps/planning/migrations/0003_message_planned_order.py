"""v0.20.1: an MRP message about a late planned order points at that planned order.

The control tower uses the link to name the planned order and propose converting it. Runs stored
before this migration are linked here once: within a run, the planner writes a late planned order
and its message one after the other, so the k-th "should have started" message of a material
belongs to the k-th late planned order of that material (both in creation order). Running the
step again links nothing new (only messages without a link are looked at), so it is idempotent.
"""
import django.db.models.deletion
from django.db import migrations, models

LATE_PREFIX = "Planned order due "


def link_existing(apps, schema_editor):
    """Pair each stored late message with its late planned order, per run and material, in order."""
    MRPMessage = apps.get_model("planning", "MRPMessage")
    PlannedOrder = apps.get_model("planning", "PlannedOrder")
    for message_run in MRPMessage.objects.filter(planned_order__isnull=True).values_list("run_id", flat=True) \
            .distinct():
        late = {}
        for order in PlannedOrder.objects.filter(run_id=message_run, is_late=True).order_by("number"):
            late.setdefault(order.material_id, []).append(order)
        for message in MRPMessage.objects.filter(run_id=message_run, planned_order__isnull=True,
                                                 text__startswith=LATE_PREFIX).order_by("id"):
            orders = late.get(message.material_id)
            if orders:
                message.planned_order = orders.pop(0)
                message.save(update_fields=["planned_order"])


class Migration(migrations.Migration):

    dependencies = [
        ("planning", "0002_mrp"),
    ]

    operations = [
        migrations.AddField(
            model_name="mrpmessage",
            name="planned_order",
            field=models.ForeignKey(
                blank=True,
                help_text="The late planned order the message is about (v0.20.1), if any.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="messages",
                to="planning.plannedorder",
            ),
        ),
        migrations.RunPython(link_existing, migrations.RunPython.noop),
    ]
