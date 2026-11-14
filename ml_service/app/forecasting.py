"""Demand forecasting for monthly quantities: a small set of proven methods, chosen per series by backtest.

Why these methods and not a neural network: ERP demand histories are short (two years is 24 points),
many are intermittent (months with no sales), and planners must be able to explain a number. Classical
methods win on such data in the M-competitions, and each one here can be explained in a sentence:

=================  ===========================================================================
``naive``          next month = last month
``moving_average`` next month = the mean of the last three months
``seasonal_naive`` next month = the same month one season ago (needs a full season of history)
``ses``            simple exponential smoothing: a level that follows the data, alpha chosen by fit
``holt_damped``    Holt's linear trend, damped (phi 0.9) so a trend fades instead of running away
``croston_sba``    Croston's method with the Syntetos-Boylan correction, for intermittent demand
=================  ===========================================================================

**Choosing a method.** Every candidate is backtested on the same rolling origins: fit on the history
up to month t, forecast month t+1, compare with what really happened, for the last ``BACKTEST``
months. The lowest mean absolute error (MAE) wins; ties go to the simpler method (the order above).
Short series (fewer than ``MIN_FOR_BACKTEST`` points) use the moving average without a backtest.

**Prediction intervals.** 80 % intervals from the backtest errors: forecast +/- z * sigma * sqrt(h),
where h is how many months ahead, never below zero (demand cannot be negative).

**Deterministic.** No randomness anywhere: the same history always gives the same forecast, which is
what makes the service safe to call again (ADR 0010, ADR 0014).

Everything here is plain numpy, with no web framework, so it can be tested and reused on its own.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

BACKTEST = 6  # rolling origins scored per method
MIN_FOR_BACKTEST = 6  # fewer points than this: moving average, no backtest
MOVING_WINDOW = 3
DAMPING = 0.9
Z80 = 1.2816  # two-sided 80 % interval of the normal distribution
ALPHAS = np.round(np.arange(0.1, 1.0, 0.1), 1)  # smoothing constants tried by SES and Holt
BETAS = np.array([0.05, 0.1, 0.2, 0.3])
CROSTON_ALPHA = 0.1  # the textbook constant for Croston: intermittent series are too noisy to fit it
INTERMITTENT_SHARE = 0.3  # Croston is a candidate only if at least this share of months had no demand

METHODS = ("naive", "moving_average", "seasonal_naive", "ses", "holt_damped", "croston_sba")


@dataclass
class Forecast:
    """One series' result: the method used, the forecast per month ahead and the backtest score."""

    method: str
    values: list[float]
    lower: list[float]
    upper: list[float]
    mae: float | None = None  # mean absolute error of the chosen method in the backtest
    mase: float | None = None  # MAE scaled by the naive method's in-sample error (< 1 = better than naive)
    backtest_points: int = 0
    scores: dict[str, float] = field(default_factory=dict)  # MAE of every candidate, for explanation


# --- the methods: each takes the history (1-D array) and a horizon, returns the forecast --------------

def naive(history: np.ndarray, horizon: int, season: int = 12) -> np.ndarray:
    """The last observed value, repeated."""
    return np.full(horizon, history[-1], dtype=float)


def moving_average(history: np.ndarray, horizon: int, season: int = 12) -> np.ndarray:
    """The mean of the last ``MOVING_WINDOW`` values, repeated."""
    return np.full(horizon, history[-MOVING_WINDOW:].mean(), dtype=float)


def seasonal_naive(history: np.ndarray, horizon: int, season: int = 12) -> np.ndarray:
    """Each future month equals the same month one season earlier."""
    last_season = history[-season:]
    return np.array([last_season[h % season] for h in range(horizon)], dtype=float)


def _ses_levels(history: np.ndarray, alphas: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Run exponential smoothing for several alphas at once (vectorised over alphas).

    Returns the final level per alpha and the sum of squared one-step errors per alpha.
    """
    level = np.full(alphas.shape, history[0], dtype=float)
    sse = np.zeros(alphas.shape, dtype=float)
    for value in history[1:]:
        error = value - level
        sse += error * error
        level = level + alphas * error
    return level, sse


def ses(history: np.ndarray, horizon: int, season: int = 12) -> np.ndarray:
    """Simple exponential smoothing; alpha is the one with the smallest in-sample one-step error."""
    levels, sse = _ses_levels(history, ALPHAS)
    return np.full(horizon, levels[int(np.argmin(sse))], dtype=float)


def holt_damped(history: np.ndarray, horizon: int, season: int = 12) -> np.ndarray:
    """Holt's linear trend with damping; alpha and beta from a small grid by in-sample fit."""
    if len(history) < 3:
        return naive(history, horizon)
    best = None
    for alpha in ALPHAS:
        for beta in BETAS:
            level, trend, sse = history[0], history[1] - history[0], 0.0
            for value in history[1:]:
                expected = level + DAMPING * trend
                sse += (value - expected) ** 2
                new_level = alpha * value + (1 - alpha) * expected
                trend = beta * (new_level - level) + (1 - beta) * DAMPING * trend
                level = new_level
            if best is None or sse < best[0]:
                best = (sse, level, trend)
    _, level, trend = best
    # Damped trend: the h-step forecast adds trend * (phi + phi^2 + ... + phi^h).
    damp = np.cumsum(DAMPING ** np.arange(1, horizon + 1))
    return level + trend * damp


def croston_sba(history: np.ndarray, horizon: int, season: int = 12) -> np.ndarray:
    """Croston's method with the Syntetos-Boylan approximation (SBA) for intermittent demand.

    Smooths the size of non-zero demands and the interval between them separately; the forecast
    rate is size / interval, times (1 - alpha / 2) to remove Croston's known upward bias.
    """
    demands = np.flatnonzero(history > 0)
    if len(demands) == 0:
        return np.zeros(horizon)
    size, interval, last = history[demands[0]], float(demands[0] + 1), demands[0]
    for index in demands[1:]:
        size += CROSTON_ALPHA * (history[index] - size)
        interval += CROSTON_ALPHA * ((index - last) - interval)
        last = index
    return np.full(horizon, (1 - CROSTON_ALPHA / 2) * size / interval, dtype=float)


FUNCTIONS = {"naive": naive, "moving_average": moving_average, "seasonal_naive": seasonal_naive, "ses": ses,
             "holt_damped": holt_damped, "croston_sba": croston_sba}


# --- choosing and scoring ----------------------------------------------------------------------------

def candidates(history: np.ndarray, season: int) -> list[str]:
    """The methods that make sense for this history, simplest first."""
    names = ["naive", "moving_average", "ses", "holt_damped"]
    if len(history) >= 2 * season:  # a season to copy, plus a season to test it on
        names.insert(2, "seasonal_naive")
    if np.mean(history == 0) >= INTERMITTENT_SHARE:
        names.append("croston_sba")
    return names


def backtest(history: np.ndarray, method: str, season: int, origins: int) -> np.ndarray:
    """One-step-ahead errors of ``method`` at the last ``origins`` months (rolling origin)."""
    function = FUNCTIONS[method]
    errors = []
    for cut in range(len(history) - origins, len(history)):
        predicted = function(history[:cut], 1, season)[0]
        errors.append(history[cut] - predicted)
    return np.asarray(errors, dtype=float)


def _naive_scale(history: np.ndarray) -> float | None:
    """In-sample MAE of the naive method: the denominator of MASE (None if the series is flat)."""
    if len(history) < 2:
        return None
    scale = float(np.mean(np.abs(np.diff(history))))
    return scale or None


def forecast(history: list[float], horizon: int, season: int = 12, method: str = "auto") -> Forecast:
    """Forecast one monthly series ``horizon`` months ahead.

    ``method`` is "auto" (choose by backtest) or one of ``METHODS`` to force a method, which is
    still backtested so its error can be reported.
    """
    data = np.asarray(history, dtype=float)
    if data.ndim != 1 or len(data) == 0:
        raise ValueError("history must be a non-empty list of numbers")
    if not np.all(np.isfinite(data)) or np.any(data < 0):
        raise ValueError("history must contain finite quantities of zero or more")
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if method != "auto" and method not in METHODS:
        raise ValueError(f"unknown method {method!r}: use auto or one of {', '.join(METHODS)}")

    if len(data) < MIN_FOR_BACKTEST:
        # Too short to judge methods: the average of what there is, with a plain spread for the interval.
        chosen = method if method != "auto" and method != "seasonal_naive" else "moving_average"
        values = FUNCTIONS[chosen](data, horizon, season)
        sigma = float(np.std(data)) if len(data) > 1 else float(data[0]) * 0.5
        return _with_interval(Forecast(chosen, values.tolist(), [], []), sigma)

    origins = min(BACKTEST, len(data) - MIN_FOR_BACKTEST // 2)
    names = candidates(data, season) if method == "auto" else [method]
    if "seasonal_naive" in names and len(data) - origins < season:
        names.remove("seasonal_naive")  # not enough history before the first origin
    if not names:
        raise ValueError(f"{method} needs at least {season + origins} months of history")
    errors = {name: backtest(data, name, season, origins) for name in names}
    scores = {name: float(np.mean(np.abs(err))) for name, err in errors.items()}
    chosen = min(names, key=lambda name: (round(scores[name], 9), names.index(name)))  # ties: simpler first

    values = FUNCTIONS[chosen](data, horizon, season)
    sigma = float(np.sqrt(np.mean(errors[chosen] ** 2)))  # root mean squared backtest error
    scale = _naive_scale(data)
    result = Forecast(chosen, values.tolist(), [], [], mae=scores[chosen],
                      mase=None if scale is None else scores[chosen] / scale, backtest_points=origins,
                      scores=scores)
    return _with_interval(result, sigma)


def _with_interval(result: Forecast, sigma: float) -> Forecast:
    """Add the 80 % interval, widening with the square root of the months ahead, never below zero."""
    values = [max(v, 0.0) for v in result.values]
    result.values = values
    result.lower = [max(v - Z80 * sigma * math.sqrt(h), 0.0) for h, v in enumerate(values, start=1)]
    result.upper = [v + Z80 * sigma * math.sqrt(h) for h, v in enumerate(values, start=1)]
    return result
