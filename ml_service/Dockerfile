# ml_service: the ERP's demand-forecasting service (Wave 8a, ADR 0014).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /service
RUN useradd --create-home --uid 1000 ml

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

USER ml
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"

# ML_SERVICE_TOKEN must be set at run time (docker compose passes it from .env); without it the
# service refuses to start.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
