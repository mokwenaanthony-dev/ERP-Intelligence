"""The forecasting contract, version 1: what the ERP sends and what it gets back (ADR 0014).

Pydantic models validate every request before any number is computed, so a bad request is refused
with a clear message (422) and never reaches the maths. The ERP repository keeps worked examples in
``ml_service/contract/`` and both sides test against them, as the UNS contract does for the MES.

Request ``demand-forecast-request/v1``::

    {"schema": "demand-forecast-request/v1", "horizon": 6, "season_length": 12,
     "series": [{"key": "1000/TG-1001", "start": "2024-11", "history": [12, 0, 15, ...]}]}

Response ``demand-forecast/v1``: per series the method chosen, its backtest error and one
``{period, quantity, lower, upper}`` per month ahead, in the order of the request.

Quantities are JSON numbers here: forecasts are estimates, so the exact-decimal rule of the ERP's own
API (ADR 0008) does not apply; the ERP rounds them to three decimals when it stores them.
"""
from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .forecasting import METHODS

REQUEST_SCHEMA = "demand-forecast-request/v1"
RESPONSE_SCHEMA = "demand-forecast/v1"
MONTH = re.compile(r"^(19|20)\d\d-(0[1-9]|1[0-2])$")
MAX_SERIES = 1000
MAX_HISTORY = 240  # twenty years of months


def add_months(month: str, count: int) -> str:
    """``"2026-11"`` plus ``count`` months, as ``YYYY-MM``."""
    year, number = int(month[:4]), int(month[5:7])
    total = year * 12 + (number - 1) + count
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


class Series(BaseModel):
    """One history to forecast: a key the caller chooses, its first month and the monthly quantities."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_./-]+$",
                     description="The caller's identifier, returned unchanged (the ERP uses plant/material).")
    start: str = Field(description="First month of the history, YYYY-MM.")
    history: list[float] = Field(min_length=1, max_length=MAX_HISTORY,
                                 description="Quantity per month from start, oldest first; zero for no demand.")
    method: str = Field("auto", description="auto (choose by backtest) or a method name to force it.")

    @field_validator("start")
    @classmethod
    def _month(cls, value: str) -> str:
        if not MONTH.match(value):
            raise ValueError("start must be a month as YYYY-MM")
        return value

    @field_validator("history")
    @classmethod
    def _quantities(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(v) or v < 0 for v in values):  # NaN and infinity are not quantities
            raise ValueError("history quantities must be finite numbers of zero or more")
        return values

    @field_validator("method")
    @classmethod
    def _method(cls, value: str) -> str:
        if value != "auto" and value not in METHODS:
            raise ValueError(f"method must be auto or one of {', '.join(METHODS)}")
        return value


class ForecastRequest(BaseModel):
    """A batch of series, forecast with the same horizon and season length."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["demand-forecast-request/v1"] = Field(alias="schema")
    horizon: int = Field(ge=1, le=24, description="Months to forecast.")
    season_length: int = Field(12, ge=2, le=24, description="Months in a season (12 for yearly patterns).")
    series: list[Series] = Field(min_length=1, max_length=MAX_SERIES)

    @model_validator(mode="after")
    def _unique_keys(self) -> "ForecastRequest":
        keys = [s.key for s in self.series]
        if len(keys) != len(set(keys)):
            raise ValueError("series keys must be unique")
        return self


class Period(BaseModel):
    """One month ahead: the forecast and its 80 % interval."""

    period: str
    quantity: float
    lower: float
    upper: float


class Backtest(BaseModel):
    """How well the chosen method did on the last months of the history."""

    mae: float | None
    mase: float | None
    points: int
    candidates: dict[str, float]


class SeriesForecast(BaseModel):
    """The result for one series."""

    key: str
    method: str
    backtest: Backtest
    forecast: list[Period]


class ForecastResponse(BaseModel):
    """The answer to a ``ForecastRequest``: one result per series, in the request's order."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: Literal["demand-forecast/v1"] = Field(RESPONSE_SCHEMA, alias="schema")
    model_version: str
    horizon: int
    series: list[SeriesForecast]
