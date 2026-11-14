"""CRM (Wave 7a): campaigns, leads, opportunities, quotations, service tickets and activities.

A lead becomes a customer and an opportunity; an accepted quotation becomes a sales order in
``apps.sales``. Rules live in ``services.py``; personal data stays out of logs and the graph (ADR 0012).
"""
