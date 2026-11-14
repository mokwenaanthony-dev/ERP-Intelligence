"""The forecasting maths and the contract, without a web server (numpy and pydantic only)."""
import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from app import forecasting
from app.contract import ForecastRequest, ForecastResponse, add_months
from app.forecasting import forecast
from app.service import handle

CONTRACT = Path(__file__).resolve().parents[1] / "contract"


def test_each_method_on_the_pattern_it_is_for():
    assert forecast([10] * 24, 3).values == [10.0, 10.0, 10.0]  # flat: naive, no error
    trend = forecast([5 + 2 * i for i in range(24)], 3)
    assert trend.method == "holt_damped" and trend.values[0] > 50 and trend.values[2] > trend.values[0]
    seasonal = [round(10 + 5 * np.sin(2 * np.pi * m / 12), 3) for m in range(36)]
    result = forecast(seasonal, 12)
    assert result.method == "seasonal_naive" and result.values == pytest.approx(seasonal[-12:])
    intermittent = [0, 0, 5, 0, 0, 0, 7, 0, 0, 4, 0, 0, 0, 6, 0, 0, 0, 5, 0, 0, 8, 0, 0, 0]
    assert forecast(intermittent, 3).method == "croston_sba"


def test_the_choice_is_explained_and_deterministic():
    history = [40, 42, 45, 41, 44, 47, 46, 49, 52, 50, 53, 55]
    first, second = forecast(history, 6), forecast(history, 6)
    assert first == second  # no randomness: the same answer every time
    assert first.scores[first.method] == min(first.scores.values()) and first.backtest_points == 6
    assert first.mase is not None and first.mase < 1  # better than the naive method


def test_intervals_widen_and_never_go_below_zero():
    result = forecast([0, 0, 6, 0, 0, 5, 0, 0, 0, 7, 0, 0], 6)
    widths = [hi - lo for lo, hi in zip(result.lower, result.upper)]
    assert all(lo >= 0 for lo in result.lower) and widths[-1] >= widths[0]


def test_short_and_empty_histories():
    assert forecast([3, 4], 2).method == "moving_average" and forecast([3, 4], 2).mae is None
    assert forecast([7], 1).values == [7.0]
    assert forecast([0] * 12, 3).values == [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match="non-empty"):
        forecast([], 3)
    with pytest.raises(ValueError, match="zero or more"):
        forecast([1, -1], 3)
    with pytest.raises(ValueError, match="horizon"):
        forecast([1, 2], 0)
    with pytest.raises(ValueError, match="unknown method"):
        forecast([1, 2], 1, method="prophet")
    with pytest.raises(ValueError, match="needs at least 18 months"):
        forecast(list(range(10)), 1, method="seasonal_naive")


def test_a_forced_method_is_still_scored():
    result = forecast([40, 42, 45, 41, 44, 47, 46, 49, 52, 50, 53, 55], 3, method="naive")
    assert result.method == "naive" and set(result.scores) == {"naive"} and result.values[0] == 55


def test_croston_helpers():
    assert forecasting.croston_sba(np.zeros(5), 2).tolist() == [0.0, 0.0]
    assert forecasting.holt_damped(np.array([1.0, 2.0]), 2).tolist() == [2.0, 2.0]  # too short: naive


def test_months():
    assert add_months("2026-11", 1) == "2026-12" and add_months("2026-11", 2) == "2027-01"
    assert add_months("2026-01", -1) == "2025-12"


def test_contract_rules():
    base = {"schema": "demand-forecast-request/v1", "horizon": 3,
            "series": [{"key": "1000/TG-1001", "start": "2026-01", "history": [1, 2]}]}
    assert ForecastRequest.model_validate(base).season_length == 12
    for change, message in (({"horizon": 25}, "less than or equal"), ({"schema": "v2"}, "demand-forecast"),
                            ({"extra": 1}, "Extra inputs")):
        with pytest.raises(ValidationError, match=message):
            ForecastRequest.model_validate({**base, **change})
    for series, message in (({"start": "2026-13"}, "YYYY-MM"), ({"history": [1, float("nan")]}, "finite"),
                            ({"key": "bad key!"}, "pattern"), ({"method": "magic"}, "method must be")):
        with pytest.raises(ValidationError, match=message):
            ForecastRequest.model_validate({**base, "series": [{**base["series"][0], **series}]})
    with pytest.raises(ValidationError, match="unique"):
        ForecastRequest.model_validate({**base, "series": base["series"] * 2})


def test_the_worked_example_in_the_contract_folder_is_what_the_service_answers():
    """The ERP tests against these two files too: if the maths changes, they must be regenerated."""
    request = json.loads((CONTRACT / "example_request.json").read_text())
    expected = json.loads((CONTRACT / "example_response.json").read_text())
    answer = handle(ForecastRequest.model_validate(request)).model_dump(by_alias=True)
    assert answer == expected
    ForecastResponse.model_validate(expected)
    assert [s["key"] for s in answer["series"]] == [s["key"] for s in request["series"]]
    assert answer["series"][0]["forecast"][0]["period"] == "2026-01"  # the month after the history
