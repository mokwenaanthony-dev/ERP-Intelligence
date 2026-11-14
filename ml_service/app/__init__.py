"""ml_service: the ERP's demand-forecasting service (Wave 8a, ADR 0014).

A separate FastAPI process because its runtime profile differs from the ERP's (numerical work, no
database), as ADR 0001 foresaw. It is stateless: it keeps nothing between requests, so the ERP stays
the system of record for histories and forecasts alike.
"""
__version__ = "0.1.0"
