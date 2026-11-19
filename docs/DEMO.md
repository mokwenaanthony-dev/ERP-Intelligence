# Demo runbook: see every wave work, end to end

Each scenario below is something the system does, the commands that show it on the Docker stack,
what to look for in the answer, and the automated test that proves the same thing on every push.
The tests are the guarantee; this runbook is how a person sees it. Run the scenarios in order on a
fresh database, or pick one: each says what it needs.

Commands are for **Git Bash on Windows** (they also work in any Bash). There are no placeholders to
fill in: where a value comes from an earlier answer, the command captures it in a variable. Every
command that changes data can be run again and gives the same result (ADR 0010).

> **Numbers vary.** Demo data is dated from today, so document numbers, amounts and dates in your
> answers will differ from the examples. What must match is the *shape* of the answer: a 403 where
> it says 403, "unchanged" on a second run, a problem with an owner and a recommendation.

## 0. Start, seed, and define the helpers

```bash
cd ~/Repos/erp-system
docker compose up -d --build
docker compose exec web python manage.py seed_data          # once per fresh database: users, demo company, every module's demo data
docker compose exec web python manage.py seed_demo_activity # optional: months of trading for the reports and dashboards
curl -s http://localhost:8000/api/health/                    # {"status": "ok", ...}
```

`seed_data` is safe to run again (it reuses what exists). The demo users are `admin`,
`finance_manager`, `accountant`, `sales_rep`, `inventory_manager`, `purchasing_officer`,
`hr_admin`, `production_planner`, `viewer`, plus portal users `highveld` (a supplier) and `acme`
(a customer), all with the local demo password `ChangeMe123!`. **Demo data only**: never reuse it.

Helpers for the API (the same as the field guide §11.2, pointed at Docker). Paste once per window:

```bash
BASE=http://localhost:8000
signin() {
  TOKEN=$(curl -s -X POST $BASE/api/auth/token/ -H "Content-Type: application/json" \
    -d '{"username":"'"$1"'","password":"ChangeMe123!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access'])")
  AUTH="Authorization: Bearer $TOKEN"
}
api() {
  local body='{}'; [ -n "$3" ] && body=$3
  if [ "$1" = GET ]; then curl -s -H "$AUTH" "$BASE$2"
  else curl -s -X "$1" -H "$AUTH" -H "Content-Type: application/json" \
         -H "Idempotency-Key: $(python -c 'import uuid;print(uuid.uuid4())')" "$BASE$2" -d "$body"; fi
}
pretty() { python -m json.tool; }
code() { curl -s -o /dev/null -w "%{http_code}\n" -H "$AUTH" "$BASE$1"; }   # only the status of a GET
```

A token lasts a limited time: if an answer is `401`, run `signin` again.

## 1. Books that always reconcile (Waves 1-3)

The Universal Journal: every module posts to one journal, so stock, receivables and payables agree
with the general ledger by construction.

```bash
signin finance_manager
api GET "/api/finance/reports/trial-balance/?company_code=1000&fiscal_year=2027" | pretty | tail -8
api GET "/api/finance/reports/receivables-reconciliation/?company_code=1000" | pretty
api GET "/api/finance/reports/payables-reconciliation/?company_code=1000" | pretty
signin inventory_manager
api GET "/api/inventory/reports/stock-reconciliation/?company_code=1000" | pretty
```

**Look for:** debits equal credits in the trial balance; a difference of `"0.00"` in each
reconciliation; amounts as exact decimal strings (`"22525.00"`, never `22525.0`, ADR 0008).
**Proved by:** `test_finance_posting.py`, `test_receivables.py`, `test_payables.py`, `test_stock_ledger.py`.

## 2. Roles: who may see what (Wave 0, POPIA)

```bash
signin sales_rep
code "/api/hr/employees/"                       # 403: salaries are HR's
signin hr_admin
code "/api/hr/employees/"                       # 200
```

**Proved by:** `test_permissions.py`, `test_hr_api.py`.

## 3. Reports and dashboards (Wave 4a)

```bash
docker compose exec web python manage.py refresh_analytics
signin finance_manager
api GET "/api/analytics/income-statement/?company_code=1000&fiscal_year=2027" | pretty | head -30
```

Dashboards: open http://localhost:3000 (Metabase), the "Management overview" dashboard.
**Look for:** `materialized` and `data_as_of` in every report: how fresh the numbers are.
**Proved by:** `test_analytics.py`.

## 4. The knowledge graph (Wave 4b)

```bash
docker compose exec web python manage.py graph_rebuild
signin sales_rep
api GET "/api/graph/customer-360/1000001/" | pretty | head -40
api GET "/api/graph/document-flow/?company_code=1000&document=SO00000001" | pretty | head -30
```

Neo4j Browser: http://localhost:7474. **Look for:** one customer across sales, billing, payments and
service in one answer; the chain order → delivery → invoice → payment.
**Proved by:** `test_graph_projection.py`, `test_graph_neo4j.py` (in CI with a real Neo4j).

## 5. Production cost and idempotency (Waves 5a-5c)

```bash
signin production_planner
api GET "/api/production/cost-estimate/?material=FG-4001&plant=1000&quantity=100" | pretty | head -20
api GET "/api/production/equipment/?plant=1000" | pretty | head -30
```

Idempotency, by sending the same request twice with the same key (ADR 0010):

```bash
signin finance_manager
KEY=$(python -c 'import uuid;print(uuid.uuid4())')
for i in 1 2; do
  curl -s -i -X POST -H "$AUTH" -H "Content-Type: application/json" -H "Idempotency-Key: $KEY" \
    "$BASE/api/analytics/refresh/" -d '{}' | grep -i -E "^HTTP|Idempotent-Replayed"
done
```

**Look for:** the ISA-95 tree with a UNS topic path per cell; the cost estimate itemised; the second
refresh answered `200` with `Idempotent-Replayed: true`: the stored first answer, nothing run again.
The same key with a different body would be refused (422).
**Proved by:** `test_production.py`, `test_production_orders.py`, `test_idempotency.py`.

## 6. Shop floor to finance through the UNS (Wave 6)

Needs the [MES-Intelligence](https://github.com/mokwenaanthony-dev/MES-Intelligence) repository and
its broker: follow its README to start Mosquitto, the MES and the Rust edge agent, then in this
repository `docker compose --profile uns up -d uns_listener`.
**Look for:** confirmations from the shop floor posted as journal entries (document type PC), and
cell state on the graph (`GET /api/graph/shop-floor/?plant=1000`).
**Proved by:** `test_uns.py`, `test_uns_state.py`, `test_uns_mqtt.py` (in CI with Mosquitto).

## 7. Customers and suppliers: CRM and SRM (Wave 7)

```bash
signin sales_rep
api GET "/api/crm/pipeline/?company_code=1000" | pretty | head -30
signin purchasing_officer
api GET "/api/srm/scorecard/?company_code=1000" | pretty | head -30
```

The full lead-to-order walkthrough is in the field guide §11.8.
**Look for:** a weighted pipeline by stage; each supplier with a grade A-D from real receipts and invoices.
**Proved by:** `test_crm.py`, `test_srm.py`.

## 8. The future state: forecast, MRP, supply risk (Wave 8)

```bash
docker compose exec web python manage.py run_forecast --plant 1000
docker compose exec web python manage.py run_mrp --plant 1000
docker compose exec web python manage.py run_mrp --plant 1000     # again: "unchanged", the same run
docker compose exec web python manage.py supply_risk --company 1000
```

**Look for:** a forecast run (FR...) with a method per material; the MRP run listing planned
orders, LATE ones flagged, and the second run reporting the same run number; Highveld HIGH risk
(sole steel supplier, grade D, no contract) with the revenue at risk.
**Proved by:** `test_planning.py` (with the ml_service contract), `test_mrp.py`, `test_risk.py`.

## 9. The control tower: AI recommends, people decide (Wave 9)

```bash
docker compose exec web python manage.py control_tower --company 1000
read -r KEY WHO <<< "$(docker compose exec -T web python manage.py shell -c "
from datetime import date
from apps.control import services
from apps.organization.models import CompanyCode
p = services.problems(CompanyCode.objects.get(code='1000'), on=date.today())[0]
print(p['key'], p['owner_role'].lower())" | tail -1)"
echo "Deciding $KEY as its owner, $WHO"
docker compose exec web python manage.py decide --company 1000 --key "$KEY" --accept --reason "Demo" --user "$WHO"
docker compose exec web python manage.py decide --company 1000 --key "$KEY" --accept --user "$WHO"
docker compose exec web python manage.py explain --company 1000 --key "$KEY" --user "$WHO"
docker compose exec web python manage.py business_rules --check
```

**Look for:**
- KPIs with target, status and the basis they were counted from ("no data" rather than a guess);
- every problem with an owner, a recommendation, a reason, an impact and a key;
- the first `decide` "Recorded", the second "Already recorded (unchanged)";
- `explain` listing numbered facts F1..F5 (and G1.. from the graph), each with its source, and
  "AI assistant: off" (it needs an administrator's switch and a provider key);
- `docs/RULES.md is current: ... rules`.

**Proved by:** `test_control.py`, `test_decisions.py`, `test_rules_catalogue.py`.

## 10. Suppliers and customers plugged in (Wave 10a)

One command plays the supplier, the company and the customer, checking every answer:

```bash
python scripts/demo_portal.py
docker compose exec web python manage.py run_mrp --plant 1000     # replans with the confirmed date
docker compose exec web python manage.py control_tower --company 1000
```

Expected (numbers and dates will differ):

```
1. Signed in as highveld: Highveld Supplies CC (1000002), a supplier
   The wall holds: internal master data answers 403 to a supplier
2. Confirmed PO00000003/10: 50.000 x TG-1001 for 2026-12-02 (agreed 2026-11-25); lines changed 1, then 0 the second time
3. Delivery notice AS00000001 (DEMO-PO00000003-10) arrives 2026-12-02; sent again: the same notice
4. Demand outlook for the next 3 months (An outlook, not an order: only a purchase order commits the company.)
   TG-1001  2026-11: ordered 0.000, planned 0.000, 2026-12: ordered 50.000, planned 0.000, ...
5. Control tower (purchasing): Confirmed later than agreed PO00000003/10
   Highveld Supplies CC confirmed 50.000 x TG-1001 for 2026-12-02, 7 day(s) after the agreed 2026-11-25
   Recommend: Accept the new date (MRP already plans with it) or ask Highveld Supplies CC to keep 2026-11-25; ...
   The warehouse sees delivery notice DEMO-PO00000003-10 in its list
6. Signed in as acme: 1 order(s), 0 late
   The wall holds for customers too: supplier pages and the control tower answer 403
Done: every answer was as expected. Run it again: the same steps give the same answers.
```

Run the script a second time: step 2 says `lines changed 0, then 0`, nothing is added. If step 2
says Highveld has no open line, run `docker compose exec web python manage.py setup_demo_portal`.
The script uses only Python's standard library, so it runs from Git Bash without the virtual
environment.

**Proved by:** `test_portal.py` (including a scan of every internal route as a supplier and a
customer) and `test_demo_script.py`, which runs this script against a live server on a freshly
seeded database, twice.

## 11. Side by side: your ERP stays, the intelligence runs on its data (Wave 10b)

The samples are an invented company's export. Each file has one row that is rejected on purpose,
to show a reason. Run each command on its own, and wait for `docker compose up` to finish before
loading: `web` creates the import tables when it starts (`migrate` below makes sure). If you load
in the wrong order, the orders are rejected with "load partners first"; load the partners, then
the same order files again, and only the rejected orders are tried:

```bash
docker compose exec web python manage.py migrate    # first, after any rebuild: the import tables must exist
for object in partners materials stock purchase_orders sales_orders; do
  docker compose exec web python manage.py import_data --company 1000 --object $object \
    --file docs/import-samples/$object.csv --user finance_manager
done
docker compose exec web python manage.py import_data --company 1000 --object partners \
  --file docs/import-samples/partners.csv --user finance_manager    # again: only the rejected row is retried
docker compose exec web python manage.py run_mrp --plant 1000      # now plans with the imported stock and orders
```

**Look for:**
- partners: 4 created, Highveld *linked to existing partner 1000002 by VAT number*, and
  `X-0401` rejected because "role must be CUSTOMER, SUPPLIER or BOTH";
- materials: `ITM-599` rejected with the units we know;
- stock: three initial entries (561) and `ITM-777` refused because it was never loaded;
- orders: `PO-X3-9003` refused (supplier `S-9999` was never loaded) and `SO-X3-7003` refused
  (Vaal Fasteners is a supplier, not a customer);
- the second load of the same file: what loaded is unchanged, and only the rejected row is tried
  again ("retried 1 rejected row(s), 0 now loaded", since its cause is still there);
- MRP planning `ITM-500` and `ITM-502` with the imported stock and orders. Highveld's portal now
  shows `PO-X3-9002`.

The layout is in [IMPORT_FORMAT.md](IMPORT_FORMAT.md).
**Proved by:** `test_connect.py`, which loads these same sample files, so the samples cannot drift
from what loads.

## When you are done

```bash
docker compose stop          # keeps the data for next time
docker compose down -v       # removes it too: the next start is a fresh database (then seed_data again)
```
