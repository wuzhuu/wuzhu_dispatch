from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.config import load_recommendation_config
from src.recommendation.dates import normalize_trade_date
from src.recommendation.evaluator import _batch_results, _net_return
from src.recommendation.performance import calculate_rolling_returns
from src.recommendation.selector import _apply_industry_cap, _batch_row
from src.recommendation.stability import _stability_rows


def _cfg(**kwargs):
    values = {
        "top_n": 10,
        "run_mode": "live_shadow",
        "raw": {},
        "holding_period_days": 30,
        "entry_price_type": "next_open",
        "weighting": "equal",
        "benchmark_index": "sh.000300",
        "max_industry_count": 3,
        "commission_rate": 0.0003,
        "minimum_commission": 5.0,
        "sell_stamp_duty_rate": 0.0005,
        "slippage_rate": 0.001,
        "position_notional": 10000,
        "batch_capital": 100000,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_low_price_stock_cost_is_reasonable():
    result = _net_return(0.0237, _cfg(position_notional=10000), weight=0.1, selected_count=10)
    assert -1.05 < result["net_return"] < 0.0237
    assert abs(result["position_notional"] - 10000) < 1e-9


def test_high_price_stock_cost_uses_notional_not_share_price():
    result = _net_return(0.02, _cfg(position_notional=10000), weight=0.1, selected_count=10)
    assert result["buy_cost_rate"] == 0.0005
    assert result["sell_cost_rate"] >= 0.0005


def test_minimum_commission_and_non_minimum_commission():
    min_case = _net_return(0.01, _cfg(position_notional=10000), weight=0.1, selected_count=10)
    large_case = _net_return(0.01, _cfg(position_notional=100000), weight=0.1, selected_count=10)
    assert min_case["buy_commission"] == 5.0
    assert large_case["buy_commission"] == 30.0


def test_stability_actual_count_and_first_nulls():
    rows = pd.DataFrame(
        [
            {"signal_date": "2025-01-01", "ts_code": "a", "rank": 1, "weight": 1 / 3, "industry_name": "x"},
            {"signal_date": "2025-01-01", "ts_code": "b", "rank": 2, "weight": 1 / 3, "industry_name": "x"},
            {"signal_date": "2025-01-01", "ts_code": "c", "rank": 3, "weight": 1 / 3, "industry_name": "y"},
            {"signal_date": "2025-01-02", "ts_code": "a", "rank": 1, "weight": 1 / 3, "industry_name": "x"},
            {"signal_date": "2025-01-02", "ts_code": "b", "rank": 2, "weight": 1 / 3, "industry_name": "x"},
            {"signal_date": "2025-01-02", "ts_code": "c", "rank": 3, "weight": 1 / 3, "industry_name": "y"},
        ]
    )
    result, details = _stability_rows(rows, "vtest", 10, None)
    assert result[0]["is_first_observation"] is True
    assert result[0]["overlap_ratio_current"] is None
    assert result[0]["weight_turnover_1d"] is None
    assert result[1]["selected_count_current"] == 3
    assert result[1]["overlap_ratio_current"] == 1.0
    assert result[1]["new_entry_ratio"] == 0.0
    assert {row["change_type"] for row in details if row["trade_date"] == pd.to_datetime("2025-01-02").date()} == {"RETAINED"}


def test_default_and_external_top_n_config():
    assert load_recommendation_config(None).top_n == 10
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recommendation.yaml"
        path.write_text("recommendation:\n  top_n: 7\n", encoding="utf-8")
        assert load_recommendation_config(path).top_n == 7


def test_invalid_top_n_has_clear_error():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recommendation.yaml"
        path.write_text("recommendation:\n  top_n: 0\n", encoding="utf-8")
        try:
            load_recommendation_config(path)
        except ValueError as exc:
            assert "recommendation.top_n" in str(exc)
        else:
            raise AssertionError("expected invalid top_n to fail")


def test_industry_cap_is_per_industry_not_total():
    cfg = _cfg(max_industry_count=3)
    rows = []
    for idx in range(8):
        rows.append({"ts_code": f"a{idx}", "industry_name": "A" if idx < 4 else "B", "final_score": 10 - idx, "total_score": 10 - idx})
    selected = _apply_industry_cap(pd.DataFrame(rows), cfg)
    assert len(selected) == 6
    assert selected["industry_name"].value_counts().max() == 3


def test_batch_target_selected_fill_ratio():
    cfg = _cfg()
    candidates = pd.DataFrame([
        {"ts_code": "a", "included": True, "exclude_reason": ""},
        {"ts_code": "b", "included": False, "exclude_reason": "LOW_LIQUIDITY"},
    ])
    selected = pd.DataFrame([{"ts_code": "a"}])
    row = _batch_row("b1", "2025-01-01", cfg, candidates, selected, "raw_top_n", "vtest", "live_shadow")
    assert row["target_top_n"] == 10
    assert row["selected_count"] == 1
    assert row["fill_ratio"] == 0.1
    assert row["quality_status"] == "WARNING"


def test_rolling_returns_require_exact_windows_and_run_mode_separation():
    rows = []
    for mode in ("backfill", "live_shadow"):
        for idx in range(253):
            rows.append({
                "trading_date": pd.Timestamp("2025-01-01") + pd.Timedelta(days=idx),
                "run_mode": mode,
                "recommendation_version": "vtest",
                "selector_name": "raw_top_n",
                "gross_daily_return": 0.001,
                "net_daily_return": 0.001,
                "benchmark_daily_return": 0.0005,
            })
    out = pd.DataFrame(calculate_rolling_returns(pd.DataFrame(rows)))
    first_live = out[(out["run_mode"] == "live_shadow")].sort_values("trading_date").iloc[19]
    monthly_live = out[(out["run_mode"] == "live_shadow")].sort_values("trading_date").iloc[20]
    annual_live = out[(out["run_mode"] == "live_shadow")].sort_values("trading_date").iloc[252]
    assert pd.isna(first_live["rolling_monthly_net_return"])
    assert monthly_live["monthly_observation_count"] == 20
    assert abs(monthly_live["rolling_monthly_net_return"] - ((1.001) ** 20 - 1)) < 1e-10
    assert annual_live["annual_observation_count"] == 252
    assert abs(annual_live["rolling_annual_net_return"] - ((1.001) ** 252 - 1)) < 1e-10
    assert set(out["run_mode"]) == {"backfill", "live_shadow"}


def test_weighted_batch_return():
    items = [
        {"batch_id": "b1", "signal_date": "2025-01-01", "ts_code": "a", "outcome_status": "COMPLETED", "gross_return_30d": 0.1, "net_return_30d": 0.09, "max_drawdown_30d": -0.01, "weight": 0.6},
        {"batch_id": "b1", "signal_date": "2025-01-01", "ts_code": "b", "outcome_status": "COMPLETED", "gross_return_30d": 0.2, "net_return_30d": 0.18, "max_drawdown_30d": -0.02, "weight": 0.3},
        {"batch_id": "b1", "signal_date": "2025-01-01", "ts_code": "c", "outcome_status": "COMPLETED", "gross_return_30d": -0.1, "net_return_30d": -0.11, "max_drawdown_30d": -0.03, "weight": 0.1},
    ]
    batches = pd.DataFrame([{"batch_id": "b1", "recommendation_version": "vtest", "selector_name": "raw_top_n"}])
    row = _batch_results(items, batches)[0]
    assert abs(row["weighted_net_return"] - (0.6 * 0.09 + 0.3 * 0.18 - 0.1 * 0.11)) < 1e-12


def test_normalize_trade_date_inputs():
    expected = pd.Timestamp("2025-01-02")
    assert normalize_trade_date("2025-01-02") == expected
    assert normalize_trade_date(dt.date(2025, 1, 2)) == expected
    assert normalize_trade_date(dt.datetime(2025, 1, 2, 13, 1)) == expected
    assert normalize_trade_date(pd.Timestamp("2025-01-02 09:30")) == expected


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            func()
            print(f"{name}: OK")
