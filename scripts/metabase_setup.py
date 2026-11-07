"""Build the ERP-Intelligence dashboards in Metabase through its REST API (dashboards as code).

Run from the project folder, with the Docker stack up and the reporting role created:

    python scripts/metabase_setup.py

    python scripts/metabase_setup.py --rebuild    # replace dashboards built earlier (after editing this file)

Reads METABASE_URL, MB_ADMIN_EMAIL, MB_ADMIN_PASSWORD, POSTGRES_DB and REPORTING_DB_PASSWORD from
.env. Safe to run again: the admin, database connection, collections and dashboards that already
exist are reused, not duplicated. Uses only the standard library.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from decouple import config  # noqa: E402

URL = config("METABASE_URL", default="http://localhost:3000").rstrip("/")
ADMIN_EMAIL = config("MB_ADMIN_EMAIL", default="admin@example.com")
ADMIN_PASSWORD = config("MB_ADMIN_PASSWORD", default="")
DATABASE_NAME = "ERP-Intelligence"
FINANCE_COLLECTION, HR_COLLECTION = "ERP-Intelligence", "ERP-Intelligence HR (restricted)"

LATEST_YEAR = "(SELECT MAX(fiscal_year) FROM analytics_gl_period_balance)"
AGE_DAYS = "CURRENT_DATE - s.last_receipt_date"
AGE = f"""CASE WHEN s.last_receipt_date IS NULL THEN 'no receipt'
              WHEN {AGE_DAYS} <= 30 THEN '0-30 days'
              WHEN {AGE_DAYS} <= 90 THEN '31-90 days'
              WHEN {AGE_DAYS} <= 180 THEN '91-180 days'
              ELSE 'over 180 days' END"""
AGE_ORDER = f"MIN(COALESCE({AGE_DAYS}, 100000))"  # oldest buckets last, "no receipt" at the end


def bar(dimension, *metrics):
    return {"graph.dimensions": [dimension], "graph.metrics": list(metrics)}


def fmt(*columns, prefix="R ", suffix="", decimals=0):
    """Number formatting per result column (Metabase keys column settings by a JSON reference)."""
    return {"column_settings": {json.dumps(["name", column]): {"prefix": prefix, "suffix": suffix,
                                                               "decimals": decimals} for column in columns}}


def chart(dimension, *metrics, **extra):
    return {**bar(dimension, *metrics), **fmt(*metrics), **extra}


# name, display, SQL, visualization settings, (col, row, width, height) on a 24-column grid
FINANCE_CARDS = [
    ("Revenue, fiscal year to date", "scalar",
     f"SELECT -SUM(balance) AS revenue FROM analytics_gl_period_balance "
     f"WHERE account_type = 'REVENUE' AND fiscal_year = {LATEST_YEAR}", fmt("revenue"), (0, 0, 8, 3)),
    ("Gross margin %, fiscal year to date", "scalar",
     "SELECT ROUND(100 * SUM(revenue - cost_of_sales) / NULLIF(SUM(revenue), 0), 1) AS gross_margin_percent "
     "FROM analytics_sales_margin WHERE fiscal_year = (SELECT MAX(fiscal_year) FROM analytics_sales_margin)",
     fmt("gross_margin_percent", prefix="", suffix="%", decimals=1), (8, 0, 8, 3)),
    ("Stock value on hand", "scalar",
     "SELECT SUM(value) AS stock_value FROM analytics_stock_position", fmt("stock_value"), (16, 0, 8, 3)),
    ("Revenue, expenses and result by period", "line",
     "SELECT fiscal_year || '-P' || LPAD(period::text, 2, '0') AS period,\n"
     "       SUM(CASE WHEN account_type = 'REVENUE' THEN -balance ELSE 0 END) AS revenue,\n"
     "       SUM(CASE WHEN account_type = 'EXPENSE' THEN balance ELSE 0 END) AS expenses,\n"
     "       -SUM(balance) AS result\n"
     "FROM analytics_gl_period_balance WHERE account_type IN ('REVENUE', 'EXPENSE')\n"
     "GROUP BY fiscal_year, period ORDER BY fiscal_year, period",
     chart("period", "revenue", "expenses", "result"), (0, 3, 24, 7)),
    ("Gross margin by customer", "row",
     "SELECT COALESCE(bp.name, 'Not assigned') AS customer, SUM(m.revenue) AS revenue,\n"
     "       SUM(m.revenue - m.cost_of_sales) AS gross_margin\n"
     "FROM analytics_sales_margin m LEFT JOIN masterdata_businesspartner bp ON bp.id = m.business_partner_id\n"
     "GROUP BY 1 ORDER BY revenue DESC",
     chart("customer", "revenue", "gross_margin"), (0, 10, 12, 7)),
    ("Gross margin by material", "row",
     "SELECT COALESCE(mat.number || ' ' || mat.description, 'Not assigned') AS material,\n"
     "       SUM(m.revenue) AS revenue, SUM(m.revenue - m.cost_of_sales) AS gross_margin\n"
     "FROM analytics_sales_margin m LEFT JOIN masterdata_material mat ON mat.id = m.material_id\n"
     "GROUP BY 1 ORDER BY revenue DESC",
     chart("material", "revenue", "gross_margin"), (12, 10, 12, 7)),
    ("Stock value by age since last receipt", "bar",
     f"SELECT {AGE} AS age, SUM(s.value) AS value\n"
     f"FROM analytics_stock_position s WHERE s.quantity <> 0 GROUP BY 1 ORDER BY {AGE_ORDER}",
     chart("age", "value"), (0, 17, 12, 7)),
    ("Slowest-moving stock", "table",
     "SELECT mat.number AS material, mat.description, p.code AS plant, s.quantity, s.value,\n"
     "       s.last_issue_date, CURRENT_DATE - s.last_issue_date AS days_since_last_issue\n"
     "FROM analytics_stock_position s\n"
     "JOIN masterdata_material mat ON mat.id = s.material_id JOIN organization_plant p ON p.id = s.plant_id\n"
     "WHERE s.quantity <> 0 ORDER BY days_since_last_issue DESC NULLS FIRST",
     fmt("value", decimals=2), (12, 17, 12, 7)),
]
HR_CARDS = [
    ("Payroll cost by month", "bar",
     "SELECT year || '-' || LPAD(month::text, 2, '0') AS month, SUM(gross) AS gross,\n"
     "       SUM(employer_contributions) AS employer_contributions, SUM(headcount) AS headcount\n"
     "FROM analytics_payroll_cost GROUP BY year, month ORDER BY year, month",
     chart("month", "gross", "employer_contributions", **{"stackable.stack_type": "stacked"}), (0, 0, 24, 7)),
    ("Payroll cost by cost centre", "row",
     "SELECT cc.code || ' ' || cc.name AS cost_center, SUM(p.total_cost) AS total_cost\n"
     "FROM analytics_payroll_cost p JOIN controlling_costcenter cc ON cc.id = p.cost_center_id\n"
     "GROUP BY 1 ORDER BY total_cost DESC",
     chart("cost_center", "total_cost"), (0, 7, 24, 6)),
]


class Metabase:
    def __init__(self, url):
        if not url.startswith(("http://", "https://")):
            raise SystemExit(f"METABASE_URL must start with http:// or https:// (got {url!r}).")
        self.url, self.session, self.hint = url, None, None

    def call(self, method, path, body=None):
        request = urllib.request.Request(f"{self.url}/api/{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
        request.add_header("Content-Type", "application/json")
        if self.session:
            request.add_header("X-Metabase-Session", self.session)
        try:
            # The scheme is checked in __init__, so only http(s) URLs are opened.
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            hint = f"\n{self.hint}" if self.hint else ""
            raise SystemExit(f"{method} /api/{path} failed: {exc.code} {exc.read().decode()[:500]}{hint}") from exc
        return json.loads(text) if text else None

    def wait_until_ready(self, minutes=5):
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            try:
                if (self.call("GET", "health") or {}).get("status") == "ok":
                    return
            except (urllib.error.URLError, ConnectionError, SystemExit):
                pass
            print("Waiting for Metabase to start...")
            time.sleep(10)
        raise SystemExit(f"Metabase did not answer at {self.url}. Is `docker compose up` running?")


def items(response):
    """Some list endpoints return a list, newer ones {"data": [...]}."""
    return response.get("data", []) if isinstance(response, dict) else response


def main():
    if not ADMIN_PASSWORD:
        raise SystemExit("Set MB_ADMIN_PASSWORD (and MB_ADMIN_EMAIL) in .env first.")
    mb = Metabase(URL)
    mb.wait_until_ready()

    properties = mb.call("GET", "session/properties")
    if not properties.get("has-user-setup"):
        mb.hint = "Metabase refused the admin details: use a stronger MB_ADMIN_PASSWORD in .env."
        mb.call("POST", "setup", {
            "token": properties["setup-token"],
            "user": {"first_name": "ERP", "last_name": "Admin", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                     "site_name": "ERP-Intelligence"},
            "prefs": {"site_name": "ERP-Intelligence", "allow_tracking": False},
        })
        print(f"Created the Metabase admin {ADMIN_EMAIL}")
    mb.hint = ("Could not sign in. If you created the Metabase admin in the browser, put that same email and "
               "password in MB_ADMIN_EMAIL and MB_ADMIN_PASSWORD in .env.")
    mb.session = mb.call("POST", "session", {"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD})["id"]
    mb.hint = None

    database = next((d for d in items(mb.call("GET", "database")) if d["name"] == DATABASE_NAME), None)
    if database is None:
        database = mb.call("POST", "database", {"engine": "postgres", "name": DATABASE_NAME, "details": {
            "host": "db", "port": 5432, "dbname": config("POSTGRES_DB", default="erp_system"),
            "user": "erp_reporting", "password": config("REPORTING_DB_PASSWORD"), "ssl": False,
        }})
        print(f"Connected Metabase to PostgreSQL as erp_reporting (database id {database['id']})")

    collections = {c["name"]: c["id"] for c in items(mb.call("GET", "collection")) if isinstance(c.get("id"), int)}
    for name in (FINANCE_COLLECTION, HR_COLLECTION):
        if name not in collections:
            collections[name] = mb.call("POST", "collection", {"name": name})["id"]

    dashboards = {d["name"]: d["id"] for d in items(mb.call("GET", "dashboard")) if not d.get("archived")}
    rebuild = "--rebuild" in sys.argv[1:]
    for title, collection, cards, description in (
        ("Management overview", FINANCE_COLLECTION, FINANCE_CARDS,
         "Revenue, margins, results and stock from the ERP analytics views."),
        ("Payroll cost", HR_COLLECTION, HR_CARDS,
         "Posted payroll by month and cost centre. Restrict this collection to finance and HR."),
    ):
        if title in dashboards:
            if not rebuild:
                print(f"Dashboard '{title}' already exists; skipped (use --rebuild to replace it)")
                continue
            old_dashboard = mb.call("GET", f"dashboard/{dashboards[title]}")
            for dashcard in old_dashboard.get("dashcards", []):
                if dashcard.get("card_id"):
                    mb.call("PUT", f"card/{dashcard['card_id']}", {"archived": True})
            mb.call("PUT", f"dashboard/{dashboards[title]}", {"archived": True})
            print(f"Moved the old '{title}' and its charts to the trash")
        dashboard = mb.call("POST", "dashboard", {"name": title, "description": description,
                                                  "collection_id": collections[collection]})
        dashcards = []
        for number, (name, display, sql, settings, (col, row, width, height)) in enumerate(cards, start=1):
            card = mb.call("POST", "card", {
                "name": name, "display": display, "collection_id": collections[collection],
                "visualization_settings": settings,
                "dataset_query": {"type": "native", "native": {"query": sql}, "database": database["id"]},
            })
            dashcards.append({"id": -number, "card_id": card["id"], "col": col, "row": row,
                              "size_x": width, "size_y": height})
        mb.call("PUT", f"dashboard/{dashboard['id']}", {"dashcards": dashcards})
        print(f"Built dashboard '{title}' with {len(cards)} charts: {URL}/dashboard/{dashboard['id']}")


if __name__ == "__main__":
    main()
