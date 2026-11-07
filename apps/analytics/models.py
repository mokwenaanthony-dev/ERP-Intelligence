"""Read-only models over the reporting views in ``sql.py`` (``managed = False``: Django never
creates or alters these tables; the views are created by migration ``0002_create_views``).

``RefreshLog`` records when each materialized view was last refreshed, so every report can say
how current its numbers are.
"""
from django.db import models

DECIMAL = {"max_digits": 18, "decimal_places": 2}


def _fk(model, null=False):
    return models.ForeignKey(model, on_delete=models.DO_NOTHING, db_constraint=False, related_name="+",
                             null=null)


class GLPeriodBalance(models.Model):
    row_key = models.CharField(max_length=120, primary_key=True)
    company_code = _fk("organization.CompanyCode")
    fiscal_year = models.PositiveSmallIntegerField()
    period = models.PositiveSmallIntegerField()
    gl_account = _fk("finance.GLAccount")
    account_type = models.CharField(max_length=10)
    profit_center = _fk("controlling.ProfitCenter", null=True)
    cost_center = _fk("controlling.CostCenter", null=True)
    debit = models.DecimalField(**DECIMAL)
    credit = models.DecimalField(**DECIMAL)
    balance = models.DecimalField(**DECIMAL)
    line_count = models.IntegerField()

    class Meta:
        managed = False
        db_table = "analytics_gl_period_balance"


class SalesMargin(models.Model):
    row_key = models.CharField(max_length=120, primary_key=True)
    company_code = _fk("organization.CompanyCode")
    fiscal_year = models.PositiveSmallIntegerField()
    period = models.PositiveSmallIntegerField()
    business_partner = _fk("masterdata.BusinessPartner", null=True)
    material = _fk("masterdata.Material", null=True)
    profit_center = _fk("controlling.ProfitCenter", null=True)
    revenue = models.DecimalField(**DECIMAL)
    cost_of_sales = models.DecimalField(**DECIMAL)

    class Meta:
        managed = False
        db_table = "analytics_sales_margin"


class StockPosition(models.Model):
    row_key = models.CharField(max_length=60, primary_key=True)
    company_code = _fk("organization.CompanyCode")
    material = _fk("masterdata.Material")
    plant = _fk("organization.Plant")
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    value = models.DecimalField(**DECIMAL)
    last_receipt_date = models.DateField(null=True)
    last_issue_date = models.DateField(null=True)

    class Meta:
        managed = False
        db_table = "analytics_stock_position"


class PayrollCost(models.Model):
    row_key = models.CharField(max_length=80, primary_key=True)
    company_code = _fk("organization.CompanyCode")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    cost_center = _fk("controlling.CostCenter")
    headcount = models.IntegerField()
    gross = models.DecimalField(**DECIMAL)
    employer_contributions = models.DecimalField(**DECIMAL)
    total_cost = models.DecimalField(**DECIMAL)

    class Meta:
        managed = False
        db_table = "analytics_payroll_cost"


class RefreshLog(models.Model):
    view_name = models.CharField(max_length=60, unique=True)
    refreshed_at = models.DateTimeField()
    duration_ms = models.PositiveIntegerField()

    class Meta:
        ordering = ["view_name"]

    def __str__(self):
        return f"{self.view_name} refreshed {self.refreshed_at:%Y-%m-%d %H:%M} ({self.duration_ms} ms)"
