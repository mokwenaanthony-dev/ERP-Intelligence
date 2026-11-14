"""The forecasting use case: a validated request in, a response out. No web framework here.

``main.py`` (FastAPI) only adds HTTP, authentication and logging around ``handle``, so the whole
service can be tested, and called from a batch job, without starting a server.
"""
from __future__ import annotations

import logging
import time

from . import __version__
from .contract import Backtest, ForecastRequest, ForecastResponse, Period, SeriesForecast, add_months
from .forecasting import forecast

logger = logging.getLogger(__name__)
DECIMALS = 3  # the ERP stores quantities with three decimals


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, DECIMALS)


def handle(request: ForecastRequest) -> ForecastResponse:
    """Forecast every series of the request. Deterministic: the same request, the same response."""
    started = time.perf_counter()
    results = []
    for series in request.series:
        result = forecast(series.history, request.horizon, request.season_length, series.method)
        first = add_months(series.start, len(series.history))  # the month after the last one given
        periods = [Period(period=add_months(first, h), quantity=_round(value), lower=_round(low), upper=_round(high))
                   for h, (value, low, high) in enumerate(zip(result.values, result.lower, result.upper))]
        results.append(SeriesForecast(
            key=series.key, method=result.method, forecast=periods,
            backtest=Backtest(mae=_round(result.mae), mase=_round(result.mase), points=result.backtest_points,
                              candidates={name: _round(score) for name, score in result.scores.items()})))
    methods = {}
    for item in results:
        methods[item.method] = methods.get(item.method, 0) + 1
    # Counts and timing only: the keys and quantities are the caller's business data.
    logger.info("Forecast %d series, %d month(s) ahead in %.0f ms; methods %s", len(results), request.horizon,
                (time.perf_counter() - started) * 1000, dict(sorted(methods.items())))
    return ForecastResponse(model_version=__version__, horizon=request.horizon, series=results)
