"""Analytics: management reporting on PostgreSQL materialized views.

Income statement, balance sheet, sales margin, stock ageing and payroll cost, read from views
that Celery beat refreshes every 15 minutes. A read model: nothing here is ever posted to.
"""
