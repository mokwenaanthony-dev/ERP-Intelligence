from rest_framework import serializers

from apps.masterdata.models import BusinessPartner, Currency, Material
from apps.organization.models import CompanyCode, Plant

from . import services
from .models import (
    ChartOfAccounts,
    DocumentType,
    FiscalYear,
    GLAccount,
    JournalEntry,
    JournalLine,
    PostingPeriod,
    TaxCode,
)

AUDIT_FIELDS = ["created_at", "updated_at", "created_by", "updated_by"]


# --- Configuration -------------------------------------------------------------------

class ChartOfAccountsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChartOfAccounts
        fields = ["id", "code", "name", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class GLAccountSerializer(serializers.ModelSerializer):
    chart_of_accounts = serializers.SlugRelatedField(slug_field="code", queryset=ChartOfAccounts.objects.all())
    is_balance_sheet = serializers.BooleanField(read_only=True)

    class Meta:
        model = GLAccount
        fields = ["id", "chart_of_accounts", "number", "name", "account_type", "reconciliation_type",
                  "is_posting_blocked", "is_balance_sheet", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]


class TaxCodeSerializer(serializers.ModelSerializer):
    chart_of_accounts = serializers.SlugRelatedField(slug_field="code", queryset=ChartOfAccounts.objects.all())
    gl_account = serializers.CharField(source="gl_account.number", required=False, allow_null=True, default=None)

    class Meta:
        model = TaxCode
        fields = ["id", "chart_of_accounts", "code", "description", "tax_type", "rate", "gl_account", *AUDIT_FIELDS]
        read_only_fields = ["id", *AUDIT_FIELDS]

    def validate(self, attrs):
        chart = attrs.get("chart_of_accounts", getattr(self.instance, "chart_of_accounts", None))
        account_data = attrs.pop("gl_account", None)
        number = account_data.get("number") if isinstance(account_data, dict) else None
        if number:
            account = GLAccount.objects.filter(chart_of_accounts=chart, number=number).first()
            if account is None:
                raise serializers.ValidationError({"gl_account": f"Account {number} is not in chart {chart.code}."})
            attrs["gl_account"] = account
        rate = attrs.get("rate", getattr(self.instance, "rate", None))
        account = attrs.get("gl_account", getattr(self.instance, "gl_account", None))
        if rate and account is None:
            raise serializers.ValidationError({"gl_account": "A tax code with a rate above 0% needs a tax account."})
        return attrs


class PostingPeriodSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="fiscal_year.company_code.code", read_only=True)
    fiscal_year = serializers.IntegerField(source="fiscal_year.year", read_only=True)

    class Meta:
        model = PostingPeriod
        fields = ["id", "company_code", "fiscal_year", "number", "start_date", "end_date", "status", "updated_at",
                  "updated_by"]
        read_only_fields = fields


class FiscalYearSerializer(serializers.ModelSerializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())
    periods = PostingPeriodSerializer(many=True, read_only=True)

    class Meta:
        model = FiscalYear
        fields = ["id", "company_code", "year", "start_date", "end_date", "periods", *AUDIT_FIELDS]
        read_only_fields = ["id", "start_date", "end_date", *AUDIT_FIELDS]
        validators = []  # duplicates are reported by the service with a clearer message

    def create(self, validated_data):
        try:
            return services.create_fiscal_year(
                validated_data["company_code"], validated_data["year"], validated_data.get("created_by")
            )
        except services.PostingError as exc:
            raise serializers.ValidationError({"year": [str(exc)]}) from exc


# --- Journal entries -----------------------------------------------------------------

class JournalLineInputSerializer(serializers.Serializer):
    gl_account = serializers.CharField(max_length=10)
    debit_credit = serializers.ChoiceField(choices=JournalLine.Side.choices)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=0)
    business_partner = serializers.CharField(required=False, allow_blank=True)
    material = serializers.CharField(required=False, allow_blank=True)
    plant = serializers.CharField(required=False, allow_blank=True)
    tax_code = serializers.CharField(required=False, allow_blank=True)
    text = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Must be greater than zero; use debit_credit for the direction.")
        return value


class JournalEntryCreateSerializer(serializers.Serializer):
    """Input for posting. Accounts, partners, materials, plants and tax codes are referenced by their codes."""

    company_code = serializers.SlugRelatedField(
        slug_field="code", queryset=CompanyCode.objects.select_related("currency", "chart_of_accounts")
    )
    document_type = serializers.ChoiceField(choices=DocumentType.choices, default=DocumentType.GENERAL)
    posting_date = serializers.DateField()
    document_date = serializers.DateField(required=False)
    currency = serializers.SlugRelatedField(slug_field="code", queryset=Currency.objects.all(), required=False)
    reference = serializers.CharField(required=False, allow_blank=True, max_length=30, default="")
    header_text = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")
    lines = JournalLineInputSerializer(many=True)

    def validate(self, attrs):
        company_code = attrs["company_code"]
        chart = company_code.chart_of_accounts
        line_inputs, line_errors = [], []
        for raw in attrs["lines"]:
            errors = {}
            account = GLAccount.objects.filter(chart_of_accounts=chart, number=raw["gl_account"]).first()
            if account is None:
                errors["gl_account"] = f"Account {raw['gl_account']} is not in chart {chart.code}."
            partner = material = plant = tax_code = None
            if raw.get("business_partner"):
                partner = BusinessPartner.objects.filter(number=raw["business_partner"]).first()
                if partner is None:
                    errors["business_partner"] = f"Business partner {raw['business_partner']} not found."
            if raw.get("material"):
                material = Material.objects.filter(number=raw["material"]).first()
                if material is None:
                    errors["material"] = f"Material {raw['material']} not found."
            if raw.get("plant"):
                plant = Plant.objects.filter(code=raw["plant"], company_code=company_code).first()
                if plant is None:
                    errors["plant"] = f"Plant {raw['plant']} not found in company code {company_code.code}."
            if raw.get("tax_code"):
                tax_code = TaxCode.objects.select_related("gl_account").filter(
                    chart_of_accounts=chart, code=raw["tax_code"]
                ).first()
                if tax_code is None:
                    errors["tax_code"] = f"Tax code {raw['tax_code']} not found in chart {chart.code}."
            line_errors.append(errors)
            if not errors:
                line_inputs.append(services.LineInput(
                    gl_account=account, debit_credit=raw["debit_credit"], amount=raw["amount"],
                    business_partner=partner, material=material, plant=plant, tax_code=tax_code,
                    text=raw.get("text", ""),
                ))
        if any(line_errors):
            raise serializers.ValidationError({"lines": line_errors})
        attrs["line_inputs"] = line_inputs
        return attrs

    def post(self, user) -> JournalEntry:
        data = self.validated_data
        try:
            return services.post_journal_entry(
                company_code=data["company_code"], document_type=data["document_type"],
                posting_date=data["posting_date"], document_date=data.get("document_date"),
                currency=data.get("currency"), reference=data["reference"], header_text=data["header_text"],
                lines=data["line_inputs"], user=user,
            )
        except services.PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


class JournalLineSerializer(serializers.ModelSerializer):
    gl_account = serializers.CharField(source="gl_account.number")
    gl_account_name = serializers.CharField(source="gl_account.name")
    currency = serializers.CharField(source="currency.code")
    local_currency = serializers.CharField(source="local_currency.code")
    business_partner = serializers.CharField(source="business_partner.number", default=None)
    material = serializers.CharField(source="material.number", default=None)
    plant = serializers.CharField(source="plant.code", default=None)
    tax_code = serializers.CharField(source="tax_code.code", default=None)

    class Meta:
        model = JournalLine
        fields = ["line_number", "gl_account", "gl_account_name", "debit_credit", "amount", "currency",
                  "amount_local", "local_currency", "business_partner", "material", "plant", "tax_code",
                  "is_tax_line", "text"]
        read_only_fields = fields


class JournalEntrySerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company_code.code")
    currency = serializers.CharField(source="currency.code")
    reversal_of = serializers.CharField(source="reversal_of.document_number", default=None)
    reversed_by = serializers.SerializerMethodField()
    lines = JournalLineSerializer(many=True)

    class Meta:
        model = JournalEntry
        fields = ["id", "company_code", "fiscal_year", "period", "document_number", "document_type",
                  "document_date", "posting_date", "reference", "header_text", "currency", "exchange_rate",
                  "status", "reversal_of", "reversed_by", "lines", "created_at", "created_by"]
        read_only_fields = fields

    def get_reversed_by(self, entry):
        reversal = getattr(entry, "reversed_by", None)  # the reverse one-to-one raises when absent
        return reversal.document_number if reversal else None


class ReverseSerializer(serializers.Serializer):
    posting_date = serializers.DateField(required=False)


# --- Receivables ------------------------------------------------------------------------

class AllocationSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)


class _PaymentSerializer(serializers.Serializer):
    company_code = serializers.SlugRelatedField(
        slug_field="code", queryset=CompanyCode.objects.select_related("currency", "chart_of_accounts")
    )
    payment_date = serializers.DateField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    bank_account = serializers.CharField(max_length=10)
    reference = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    header_text = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    allocations = AllocationSerializer(many=True, required=False)

    partner_field = ""  # "customer" or "supplier"
    service = None

    def validate(self, attrs):
        chart = attrs["company_code"].chart_of_accounts
        account = GLAccount.objects.filter(chart_of_accounts=chart, number=attrs["bank_account"]).first()
        if account is None:
            raise serializers.ValidationError({"bank_account": f"Account {attrs['bank_account']} is not in chart "
                                                               f"{chart.code}."})
        attrs["bank_account"] = account
        return attrs

    def post(self, user) -> JournalEntry:
        data = self.validated_data
        allocations = data.get("allocations")
        try:
            return type(self).service(
                company_code=data["company_code"], payment_date=data["payment_date"],
                amount=data["amount"], bank_account=data["bank_account"], reference=data["reference"],
                header_text=data["header_text"], user=user, **{self.partner_field: data[self.partner_field]},
                allocations=[(a["line_id"], a["amount"]) for a in allocations] if allocations is not None else None,
            )
        except services.PostingError as exc:
            raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


class IncomingPaymentSerializer(_PaymentSerializer):
    """A customer payment. Without allocations it clears the customer's oldest open items first."""

    customer = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    partner_field = "customer"
    service = staticmethod(services.post_incoming_payment)


class OutgoingPaymentSerializer(_PaymentSerializer):
    """A payment to a supplier. Without allocations it clears the supplier's oldest open items first."""

    supplier = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    partner_field = "supplier"
    service = staticmethod(services.post_outgoing_payment)
