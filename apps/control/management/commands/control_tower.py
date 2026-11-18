"""``manage.py control_tower --company 1000``: the KPIs, the problems with their recommendations, and
the track record of the decisions.

Read-only; the same calculations as ``GET /api/control/kpis/``, ``/recommendations/`` and
``/acceptance/``. Each problem shows its key, which ``manage.py decide`` and ``manage.py explain`` take.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.common.logcontext import command_request_id
from apps.control import decisions, services
from apps.organization.models import CompanyCode

MARK = {"good": "OK  ", "warning": "WARN", "critical": "CRIT", None: "  - "}


class Command(BaseCommand):
    help = "Show the control-tower KPIs and the problems inbox for a company code."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)

    def handle(self, *args, **options):
        """Run the command under its own request ID, restored afterwards (no leak into what runs next)."""
        with command_request_id():
            return self._run(*args, **options)

    def _run(self, *args, company, **options):
        company_code = CompanyCode.objects.select_related("currency").filter(code=company).first()
        if company_code is None:
            raise CommandError(f"Company code {company} does not exist.")
        on = timezone.localdate()
        result = services.kpis(company_code, on=on)
        self.stdout.write(self.style.MIGRATE_HEADING(f"State of company {company} on {on}"))
        for kpi in result["kpis"]:
            value = "no data" if kpi["value"] is None else f"{kpi['value']} {kpi['unit']}"
            style = {"good": self.style.SUCCESS, "warning": self.style.WARNING,
                     "critical": self.style.ERROR}.get(kpi["status"], str)
            self.stdout.write(style(f"  {MARK[kpi['status']]} {kpi['name']:<28} {value:>16}  (target "
                                    f"{kpi['target']}; {kpi['basis']})"))
        future = result["future"]
        self.stdout.write(self.style.MIGRATE_HEADING("Ahead"))
        self.stdout.write(f"  Open order book (next {result['months']} months): R{future['open_order_book']}")
        self.stdout.write(f"  Planned orders that should already have started: {future['late_planned_orders']}")
        self.stdout.write(f"  Suppliers at high risk: {future['suppliers_at_high_risk']}")
        found = decisions.recommendations(company_code, on=on)
        self.stdout.write(self.style.MIGRATE_HEADING(f"Problems ({len(found)})"))
        for problem in found:
            style = self.style.ERROR if problem["severity"] == "critical" else self.style.WARNING
            self.stdout.write(style(f"  {problem['severity'][:4].upper():<4} [{problem['area']}] {problem['title']} "
                                    f"-> {problem['owner']}"))
            self.stdout.write(f"       {problem['detail']}")
            advice, decision = problem["recommendation"], problem["decision"]
            self.stdout.write(f"       Recommend: {advice['action']}")
            self.stdout.write(f"       Why: {advice['reason']} Impact: {advice['impact']['text']}")
            state = (f"{decision['outcome'].lower()} by {decision['decided_by']}" if decision else "open")
            self.stdout.write(f"       Key: {problem['key']}  [{state}]")
        record = decisions.acceptance(company_code, on=on)
        self.stdout.write(self.style.MIGRATE_HEADING("Decisions (last 90 days)"))
        self.stdout.write(f"  {record['decided']} decided, {record['accepted']} accepted "
                          f"({record['acceptance_rate'] if record['acceptance_rate'] is not None else '-'} %), "
                          f"{record['open_recommendations']} open")
        for row in record["by_kind"]:
            self.stdout.write(f"  {row['kind']:<14} {row['accepted']}/{row['decided']} accepted, "
                              f"{row['accepted_resolved']} resolved since")
