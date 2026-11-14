"""Serializers for planning: validate API input and shape forecast runs for output.

Business rules run in ``services.py`` (called from ``post()``), so a broken rule keeps its cause and
the log shows its file and line; it comes back as a 400 with the rule's message.
"""
from rest_framework import serializers

from apps.finance.services import PostingError
from apps.masterdata.models import Material
from apps.organization.models import Plant

from . import services
from .models import ForecastItem, ForecastRun

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


def run(func, *args, **kwargs):
    """Call a service; a broken rule becomes a 400 (the cause is kept, so the log shows file and line)."""
    try:
        return func(*args, **kwargs)
    except PostingError as exc:
        raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


class MonthField(serializers.DateField):
    """A month as ``YYYY-MM`` (or any date in it); always the first day of the month."""

    def to_internal_value(self, value):
        """Accept ``2026-12`` or ``2026-12-15``; return 2026-12-01."""
        if isinstance(value, str) and len(value) == 7:
            value = f"{value}-01"
        return services.month_start(super().to_internal_value(value))


class ForecastRequestSerializer(serializers.Serializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.select_related("company_code"))
    first_period = MonthField(help_text="First month to forecast, YYYY-MM.")
    horizon = serializers.IntegerField(min_value=1, max_value=services.MAX_HORIZON, default=6)
    history_months = serializers.IntegerField(min_value=services.HISTORY_RANGE[0],
                                              max_value=services.HISTORY_RANGE[1], default=24)
    materials = serializers.SlugRelatedField(slug_field="number", queryset=Material.objects.all(), many=True,
                                             required=False, help_text="Leave out: every material with demand.")

    def post(self, user):
        data = self.validated_data
        return run(services.run_forecast, data["plant"], first_period=data["first_period"], horizon=data["horizon"],
                   history_months=data["history_months"], materials=data.get("materials"), user=user)


def _periods(item):
    return [{"period": f"{p.period:%Y-%m}", "quantity": p.quantity, "lower": p.lower, "upper": p.upper}
            for p in item.periods.all()]


class ForecastItemSerializer(serializers.ModelSerializer):
    material = serializers.CharField(source="material.number")
    description = serializers.CharField(source="material.description")
    periods = serializers.SerializerMethodField()

    class Meta:
        model = ForecastItem
        fields = ["material", "description", "method", "mae", "mase", "history_total", "periods"]
        read_only_fields = fields

    def get_periods(self, item):
        return _periods(item)


class ForecastRunSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    plant = serializers.CharField(source="plant.code")
    first_period = serializers.DateField(format="%Y-%m")
    items = ForecastItemSerializer(many=True)

    class Meta:
        model = ForecastRun
        fields = ["id", "run_number", "company_code", "plant", "first_period", "horizon", "history_months",
                  "model_version", "items", *AUDIT_FIELDS]
        read_only_fields = fields


class ForecastRunListSerializer(ForecastRunSerializer):
    materials = serializers.IntegerField(source="items.count")

    class Meta(ForecastRunSerializer.Meta):
        fields = ["id", "run_number", "company_code", "plant", "first_period", "horizon", "history_months",
                  "model_version", "materials", *AUDIT_FIELDS]
        read_only_fields = fields


class LatestForecastSerializer(ForecastItemSerializer):
    run_number = serializers.CharField(source="run.run_number")
    plant = serializers.CharField(source="run.plant.code")

    class Meta(ForecastItemSerializer.Meta):
        fields = ["run_number", "plant", *ForecastItemSerializer.Meta.fields]
        read_only_fields = fields
