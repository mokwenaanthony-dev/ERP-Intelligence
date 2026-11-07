from rest_framework import serializers

from apps.organization.models import CompanyCode, Plant

from .models import CostCenter, PlantProfitCenter, ProfitCenter

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


class ProfitCenterSerializer(serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())

    class Meta:
        model = ProfitCenter
        fields = ["id", "company_code", "code", "name", "is_active", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class CostCenterSerializer(serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    profit_center = serializers.CharField(source="profit_center.code")

    class Meta:
        model = CostCenter
        fields = ["id", "company_code", "code", "name", "category", "profit_center", "manager", "is_active",
                  *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        company_code = attrs.get("company_code", getattr(self.instance, "company_code", None))
        if "profit_center" in attrs:
            code = attrs.pop("profit_center")["code"]
            profit_center = ProfitCenter.objects.filter(company_code=company_code, code=code).first()
            if profit_center is None:
                raise serializers.ValidationError(
                    {"profit_center": f"Profit centre {code} not found in company code {company_code.code}."}
                )
            attrs["profit_center"] = profit_center
        elif self.instance and company_code != self.instance.company_code:
            raise serializers.ValidationError({"profit_center": "Give the profit centre in the new company code."})
        return attrs


class PlantProfitCenterSerializer(serializers.ModelSerializer):
    plant = serializers.SlugRelatedField(slug_field="code", queryset=Plant.objects.all())
    profit_center = serializers.PrimaryKeyRelatedField(queryset=ProfitCenter.objects.all())

    class Meta:
        model = PlantProfitCenter
        fields = ["id", "plant", "profit_center", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        plant = attrs.get("plant", getattr(self.instance, "plant", None))
        profit_center = attrs.get("profit_center", getattr(self.instance, "profit_center", None))
        if plant.company_code_id != profit_center.company_code_id:
            raise serializers.ValidationError({"profit_center": "Plant and profit centre must be in the same "
                                                                "company code."})
        return attrs
