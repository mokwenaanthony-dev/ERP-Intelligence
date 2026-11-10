"""Production orders, confirmations and their planned values (Wave 5b)."""

import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0006_journalline_order_number"),
        ("masterdata", "0004_alter_material_material_type"),
        ("organization", "0001_initial"),
        ("production", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Confirmation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("confirmation_number", models.CharField(max_length=20)),
                ("posting_date", models.DateField()),
                (
                    "yield_quantity",
                    models.DecimalField(
                        decimal_places=3, default=Decimal("0"), max_digits=15
                    ),
                ),
                (
                    "scrap_quantity",
                    models.DecimalField(
                        decimal_places=3, default=Decimal("0"), max_digits=15
                    ),
                ),
                (
                    "is_final",
                    models.BooleanField(
                        default=False,
                        help_text="No more work expected on this operation.",
                    ),
                ),
                (
                    "external_id",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="ID from the sending system (e.g. the MES event ID); repeats are refused.",
                        max_length=64,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_%(class)s_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "journal_entry",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="production_confirmation",
                        to="finance.journalentry",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_%(class)s_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["order", "confirmation_number"],
            },
        ),
        migrations.CreateModel(
            name="ConfirmationActivity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=12)),
                ("rate", models.DecimalField(decimal_places=2, max_digits=15)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                (
                    "activity_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="production.activitytype",
                    ),
                ),
                (
                    "confirmation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="production.confirmation",
                    ),
                ),
            ],
            options={
                "ordering": ["confirmation", "activity_type__code"],
            },
        ),
        migrations.CreateModel(
            name="OrderOperation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("operation_number", models.PositiveIntegerField()),
                ("description", models.CharField(max_length=100)),
                (
                    "work_center",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_operations",
                        to="production.workcenter",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "operation_number"],
            },
        ),
        migrations.AddField(
            model_name="confirmation",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="confirmations",
                to="production.orderoperation",
            ),
        ),
        migrations.CreateModel(
            name="ProductionOrder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order_number", models.CharField(max_length=20)),
                (
                    "quantity",
                    models.DecimalField(
                        decimal_places=3,
                        max_digits=15,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0.001"))
                        ],
                    ),
                ),
                ("start_date", models.DateField()),
                ("finish_date", models.DateField()),
                (
                    "planned_cost",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Standard cost estimate for the order quantity, at creation.",
                        max_digits=18,
                    ),
                ),
                (
                    "planned_unit_cost",
                    models.DecimalField(
                        decimal_places=4,
                        help_text="Value per unit of finished goods received from the order.",
                        max_digits=18,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("CREATED", "Created"),
                            ("RELEASED", "Released"),
                            ("COMPLETED", "Completed and settled"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="CREATED",
                        max_length=10,
                    ),
                ),
                ("released_at", models.DateField(blank=True, null=True)),
                ("completed_at", models.DateField(blank=True, null=True)),
                (
                    "bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="production.billofmaterials",
                    ),
                ),
                (
                    "company_code",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="organization.companycode",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_%(class)s_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="production_orders",
                        to="masterdata.material",
                    ),
                ),
                (
                    "plant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="production_orders",
                        to="organization.plant",
                    ),
                ),
                (
                    "receipt_storage_location",
                    models.ForeignKey(
                        help_text="Where the finished goods go.",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="organization.storagelocation",
                    ),
                ),
                (
                    "routing",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="production.routing",
                    ),
                ),
                (
                    "settlement_entry",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="settled_order",
                        to="finance.journalentry",
                    ),
                ),
                (
                    "unit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="masterdata.unitofmeasure",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_%(class)s_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["company_code__code", "-order_number"],
            },
        ),
        migrations.AddField(
            model_name="orderoperation",
            name="order",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="operations",
                to="production.productionorder",
            ),
        ),
        migrations.CreateModel(
            name="OrderComponent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("item_number", models.PositiveIntegerField()),
                (
                    "required_quantity",
                    models.DecimalField(decimal_places=3, max_digits=15),
                ),
                (
                    "planned_value",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0.00"),
                        help_text="From the cost estimate at creation.",
                        max_digits=18,
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="masterdata.material",
                    ),
                ),
                (
                    "storage_location",
                    models.ForeignKey(
                        help_text="Default storage location to issue from.",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="organization.storagelocation",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="components",
                        to="production.productionorder",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "item_number"],
            },
        ),
        migrations.AddField(
            model_name="confirmation",
            name="order",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="confirmations",
                to="production.productionorder",
            ),
        ),
        migrations.CreateModel(
            name="OrderOperationActivity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "planned_quantity",
                    models.DecimalField(decimal_places=3, max_digits=12),
                ),
                (
                    "planned_value",
                    models.DecimalField(
                        decimal_places=2, default=Decimal("0.00"), max_digits=18
                    ),
                ),
                (
                    "activity_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="production.activitytype",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="production.orderoperation",
                    ),
                ),
            ],
            options={
                "ordering": ["operation", "activity_type__code"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("operation", "activity_type"),
                        name="unique_order_operation_activity",
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="productionorder",
            constraint=models.UniqueConstraint(
                fields=("company_code", "order_number"),
                name="unique_production_order_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderoperation",
            constraint=models.UniqueConstraint(
                fields=("order", "operation_number"), name="unique_order_operation"
            ),
        ),
        migrations.AddConstraint(
            model_name="ordercomponent",
            constraint=models.UniqueConstraint(
                fields=("order", "item_number"), name="unique_order_component"
            ),
        ),
        migrations.AddConstraint(
            model_name="confirmation",
            constraint=models.UniqueConstraint(
                fields=("order", "confirmation_number"),
                name="unique_confirmation_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="confirmation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("external_id", ""), _negated=True),
                fields=("order", "external_id"),
                name="unique_confirmation_external_id",
            ),
        ),
    ]
