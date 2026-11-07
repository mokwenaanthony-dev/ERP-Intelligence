"""Reporting views over the transactional tables.

Each view is one SELECT written in plain SQL that both databases understand. On PostgreSQL it is
created as a MATERIALIZED VIEW (precomputed, refreshed by ``services.refresh_views``); on SQLite,
used for quick local work and tests, it is an ordinary view that is always current.

Every view has a ``row_key`` column: a unique text key built from the grouping columns. It is the
Django primary key of the read-only model and the unique index PostgreSQL needs to refresh a
materialized view CONCURRENTLY (readers are never blocked).

Changing a table a view reads from: drop the views first and create them again afterwards, in the
same migration (``drop_views`` / ``create_views``), because PostgreSQL will not alter a column a view
depends on.
"""


# Journal balances per company code, fiscal year, period, account, profit centre and cost centre:
# the base of the income statement and balance sheet.
GL_PERIOD_BALANCE = """
SELECT
    CAST(COALESCE(jl.company_code_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.fiscal_year, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.period, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.gl_account_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.profit_center_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.cost_center_id, 0) AS VARCHAR(20))
    AS row_key,
    jl.company_code_id AS company_code_id,
    jl.fiscal_year AS fiscal_year,
    jl.period AS period,
    jl.gl_account_id AS gl_account_id,
    ga.account_type AS account_type,
    jl.profit_center_id AS profit_center_id,
    jl.cost_center_id AS cost_center_id,
    SUM(CASE WHEN jl.amount_local > 0 THEN jl.amount_local ELSE 0 END) AS debit,
    SUM(CASE WHEN jl.amount_local < 0 THEN -jl.amount_local ELSE 0 END) AS credit,
    SUM(jl.amount_local) AS balance,
    COUNT(*) AS line_count
FROM finance_journalline jl
JOIN finance_glaccount ga ON ga.id = jl.gl_account_id
GROUP BY jl.company_code_id, jl.fiscal_year, jl.period, jl.gl_account_id, ga.account_type,
         jl.profit_center_id, jl.cost_center_id
"""

# Revenue and cost of sales per customer, material and profit centre, straight from the journal
# (margin analysis the S/4HANA way). Revenue is recognised on the invoice, cost of sales on the
# goods issue, so a period can show cost before revenue for goods delivered but not yet billed.
SALES_MARGIN = """
SELECT
    CAST(COALESCE(jl.company_code_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.fiscal_year, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.period, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.business_partner_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.material_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(jl.profit_center_id, 0) AS VARCHAR(20))
    AS row_key,
    jl.company_code_id AS company_code_id,
    jl.fiscal_year AS fiscal_year,
    jl.period AS period,
    jl.business_partner_id AS business_partner_id,
    jl.material_id AS material_id,
    jl.profit_center_id AS profit_center_id,
    SUM(CASE WHEN ga.account_type = 'REVENUE' THEN -jl.amount_local ELSE 0 END) AS revenue,
    SUM(CASE WHEN ga.account_type = 'REVENUE' THEN 0 ELSE jl.amount_local END) AS cost_of_sales
FROM finance_journalline jl
JOIN finance_glaccount ga ON ga.id = jl.gl_account_id
WHERE ga.account_type = 'REVENUE'
   OR jl.gl_account_id IN (
        SELECT gl_account_id FROM inventory_inventoryaccountsetting WHERE account_key = 'COST_OF_SALES'
   )
GROUP BY jl.company_code_id, jl.fiscal_year, jl.period, jl.business_partner_id, jl.material_id,
         jl.profit_center_id
"""

# Stock on hand per material and plant, with the dates of the last real receipt and issue
# (reversal documents and transfers between storage locations are ignored for the dates).
STOCK_POSITION = """
SELECT
    CAST(COALESCE(mdi.material_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(mdi.plant_id, 0) AS VARCHAR(20))
    AS row_key,
    p.company_code_id AS company_code_id,
    mdi.material_id AS material_id,
    mdi.plant_id AS plant_id,
    SUM(mdi.quantity) AS quantity,
    SUM(mdi.amount_local) AS value,
    MAX(CASE WHEN md.movement_type IN ('101', '561', '701') AND md.reversal_of_id IS NULL
             THEN md.posting_date END) AS last_receipt_date,
    MAX(CASE WHEN md.movement_type IN ('601', '551', '702') AND md.reversal_of_id IS NULL
             THEN md.posting_date END) AS last_issue_date
FROM inventory_materialdocumentitem mdi
JOIN inventory_materialdocument md ON md.id = mdi.document_id
JOIN organization_plant p ON p.id = mdi.plant_id
GROUP BY p.company_code_id, mdi.material_id, mdi.plant_id
"""

# Posted payroll per company code, month and cost centre. Reversed runs are left out.
PAYROLL_COST = """
SELECT
    CAST(COALESCE(run.company_code_id, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(run.year, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(run.month, 0) AS VARCHAR(20))
    || '-' || CAST(COALESCE(res.cost_center_id, 0) AS VARCHAR(20))
    AS row_key,
    run.company_code_id AS company_code_id,
    run.year AS year,
    run.month AS month,
    res.cost_center_id AS cost_center_id,
    COUNT(*) AS headcount,
    SUM(res.gross) AS gross,
    SUM(res.uif_employer + res.sdl) AS employer_contributions,
    SUM(res.gross + res.uif_employer + res.sdl) AS total_cost
FROM hr_payrollresult res
JOIN hr_payrollrun run ON run.id = res.run_id
WHERE run.status = 'POSTED'
GROUP BY run.company_code_id, run.year, run.month, res.cost_center_id
"""

# name -> (SELECT, extra indexed columns for PostgreSQL)
VIEWS = {
    "analytics_gl_period_balance": (GL_PERIOD_BALANCE, ["company_code_id, fiscal_year, period"]),
    "analytics_sales_margin": (SALES_MARGIN, ["company_code_id, fiscal_year, period"]),
    "analytics_stock_position": (STOCK_POSITION, ["company_code_id"]),
    "analytics_payroll_cost": (PAYROLL_COST, ["company_code_id, year, month"]),
}


def is_postgres(connection) -> bool:
    return connection.vendor == "postgresql"


def create_views(connection) -> None:
    materialized = is_postgres(connection)
    kind = "MATERIALIZED VIEW" if materialized else "VIEW"
    with connection.cursor() as cursor:
        for name, (select, indexes) in VIEWS.items():
            # Names and SQL are module constants, never user input.
            cursor.execute(f"CREATE {kind} {name} AS {select}")  # nosec B608
            if materialized:
                cursor.execute(f"CREATE UNIQUE INDEX {name}_row_key ON {name} (row_key)")
                for number, columns in enumerate(indexes, start=1):
                    cursor.execute(f"CREATE INDEX {name}_idx{number} ON {name} ({columns})")


def drop_views(connection) -> None:
    kind = "MATERIALIZED VIEW" if is_postgres(connection) else "VIEW"
    with connection.cursor() as cursor:
        for name in reversed(list(VIEWS)):
            cursor.execute(f"DROP {kind} IF EXISTS {name}")
