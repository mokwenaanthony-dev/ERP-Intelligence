"""Planning (Wave 8a): demand history from deliveries, and demand forecasts from ``ml_service``.

The ERP builds each material's monthly demand from its posted goods issues for deliveries (601),
sends the histories to the forecasting service (``ml_service``, FastAPI, ADR 0014) and keeps the
answer as a numbered forecast run. MRP (Wave 8b) will plan against these forecasts.
"""
