"""SRM (Wave 7b): supplier onboarding, requests for quotation, contracts and supplier evaluation.

An approved application becomes a supplier (Business Partner with a supplier role); an awarded
quotation and a contract release become purchase orders in ``apps.purchasing``. Rules live in
``services.py``; applicants' contact details stay out of logs and the graph (ADR 0013).
"""
