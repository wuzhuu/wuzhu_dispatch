from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.utils.config import load_yaml


@dataclass(frozen=True)
class RecommendationConfig:
    enabled: bool = True
    recommendation_version: str = "v1"
    selector_name: str = "raw_top_n"
    selector_names: tuple[str, ...] = ("raw_top_n", "confirmed_top_n", "low_turnover_top_n")
    holding_period_days: int = 30
    top_n: int = 10
    run_mode: str = "live_shadow"
    entry_price_type: str = "next_open"
    exit_price_type: str = "close"
    weighting: str = "equal"
    benchmark_index: str = "sh.000300"
    initial_capital: float = 1_000_000
    batch_capital: float = 100_000
    position_notional: float = 10_000
    min_history_days: int = 120
    min_amount_ma20: float = 50_000_000
    min_valid_days_20: int = 15
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_new_listing_days: int = 120
    allowed_risk_levels: tuple[str, ...] = ("LOW", "MEDIUM")
    max_industry_count: int = 3
    score_weight: float = 0.55
    risk_weight: float = 0.20
    industry_strength_weight: float = 0.10
    news_weight: float = 0.15
    require_news_signal: bool = False
    missing_news_policy: str = "neutral"
    confirmed_entry_top_n: int = 15
    confirmed_entry_days: int = 2
    confirmed_exit_top_n: int = 30
    confirmed_exit_days: int = 2
    replacement_score_gap: float = 0.03
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    sell_stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.001
    update_daily: bool = True
    allow_overlapping_batches: bool = True
    calculate_multi_period_returns: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


def _positive_int(section: dict[str, Any], key: str, default: int, label: str) -> int:
    value = section.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer >= 1, got {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be an integer >= 1, got {value!r}")
    return parsed


def load_recommendation_config(path: str | Path | None) -> RecommendationConfig:
    data = load_yaml(path) if path else {}
    rec = data.get("recommendation", {})
    universe = data.get("universe", {})
    scoring = data.get("scoring", {})
    costs = data.get("costs", {})
    tracking = data.get("tracking", {})
    stability = data.get("stability", {})
    return RecommendationConfig(
        enabled=bool(rec.get("enabled", True)),
        recommendation_version=str(rec.get("recommendation_version", "v1")),
        selector_name=str(rec.get("selector_name", "raw_top_n")),
        selector_names=tuple(str(x) for x in rec.get("selector_names", ["raw_top_n", "confirmed_top_n", "low_turnover_top_n"])),
        holding_period_days=int(rec.get("holding_period_days", 30)),
        top_n=_positive_int(rec, "top_n", 10, "recommendation.top_n"),
        run_mode=str(rec.get("run_mode", "live_shadow")),
        entry_price_type=str(rec.get("entry_price_type", "next_open")),
        exit_price_type=str(rec.get("exit_price_type", "close")),
        weighting=str(rec.get("weighting", "equal")),
        benchmark_index=str(rec.get("benchmark_index", "sh.000300")),
        initial_capital=float(rec.get("portfolio", {}).get("initial_capital", 1_000_000)),
        batch_capital=float(rec.get("portfolio", {}).get("batch_capital", 100_000)),
        position_notional=float(rec.get("portfolio", {}).get("position_notional", 10_000)),
        min_history_days=int(universe.get("min_history_days", 120)),
        min_amount_ma20=float(universe.get("min_amount_ma20", 50_000_000)),
        min_valid_days_20=int(universe.get("min_valid_days_20", 15)),
        exclude_st=bool(universe.get("exclude_st", True)),
        exclude_suspended=bool(universe.get("exclude_suspended", True)),
        exclude_new_listing_days=int(universe.get("exclude_new_listing_days", 120)),
        allowed_risk_levels=tuple(str(x) for x in universe.get("allowed_risk_levels", ["LOW", "MEDIUM"])),
        max_industry_count=int(universe.get("max_industry_count", 3)),
        score_weight=float(scoring.get("score_weight", 0.55)),
        risk_weight=float(scoring.get("risk_weight", 0.20)),
        industry_strength_weight=float(scoring.get("industry_strength_weight", 0.10)),
        news_weight=float(scoring.get("news_weight", 0.15)),
        require_news_signal=bool(scoring.get("require_news_signal", False)),
        missing_news_policy=str(scoring.get("missing_news_policy", "neutral")),
        confirmed_entry_top_n=int(stability.get("confirmed_entry_top_n", 15)),
        confirmed_entry_days=int(stability.get("confirmed_entry_days", 2)),
        confirmed_exit_top_n=int(stability.get("confirmed_exit_top_n", 30)),
        confirmed_exit_days=int(stability.get("confirmed_exit_days", 2)),
        replacement_score_gap=float(stability.get("replacement_score_gap", 0.03)),
        commission_rate=float(costs.get("commission_rate", 0.0003)),
        minimum_commission=float(costs.get("minimum_commission", 5.0)),
        sell_stamp_duty_rate=float(costs.get("sell_stamp_duty_rate", 0.0005)),
        slippage_rate=float(costs.get("slippage_rate", 0.001)),
        update_daily=bool(tracking.get("update_daily", True)),
        allow_overlapping_batches=bool(tracking.get("allow_overlapping_batches", True)),
        calculate_multi_period_returns=bool(tracking.get("calculate_multi_period_returns", True)),
        raw=data,
    )
