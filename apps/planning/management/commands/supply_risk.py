"""``manage.py supply_risk --company 1000 [--months 3]``: revenue at risk per supplier, with recommendations.

Read-only. The same calculation as ``GET /api/planning/supplier-risk/`` (``apps.planning.risk``).
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.organization.models import CompanyCode
from apps.planning import risk


class Command(BaseCommand):
    help = "Show revenue at risk per supplier, with a risk level and a recommendation."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--months", type=int, default=3, help="Open sales orders wanted within this horizon.")

    def handle(self, *args, company, months=3, **options):
        company_code = CompanyCode.objects.filter(code=company).first()
        if company_code is None:
            raise CommandError(f"Company code {company} does not exist.")
        rows = risk.supplier_risk(company_code, on=timezone.localdate(), months=months)
        for row in rows:
            style = {"HIGH": self.style.ERROR, "MEDIUM": self.style.WARNING}.get(row["risk"], self.style.SUCCESS)
            self.stdout.write(style(f"{row['risk']:<6} {row['supplier']} {row['name']} (grade {row['grade'] or '-'}): "
                                    f"revenue at risk R{row['revenue_at_risk']} of R{row['exposed_revenue']} exposed"))
            for line in row["materials"]:
                flags = ", ".join(f for f, on in (("single source", line["single_source"]),
                                                  ("on contract", line["on_contract"])) if on) or "second source"
                used = f"; used in {', '.join(line['used_in'])}" if line["used_in"] else ""
                self.stdout.write(f"         {line['material']}: {flags}{used}")
            self.stdout.write(f"         -> {row['recommendation']}")
        if not rows:
            self.stdout.write("No suppliers found for this company code.")
