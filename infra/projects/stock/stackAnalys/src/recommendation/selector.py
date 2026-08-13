from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from .config import RecommendationConfig, load_recommendation_config
from .dates import trade_date
from .stores import RecommendationStore
from .universe import is_st_name, normalize_0_1, risk_score


def generate_recommendations(
    db_path: str | Path | None,
    config_path: str | Path | None,
    target_date: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    selector_name: str | None = None,
) -> list[str]:
    cfg = load_recommendation_config(config_path)
    selector_name = selector_name or cfg.selector_name
    recommendation_version = _effective_version(cfg, selector_name)
    rec = RecommendationStore(db_path)
    lines: list[str] = [f"recommendation_version: {recommendation_version}", f"selector_name: {selector_name}", f"enabled: {cfg.enabled}"]
    try:
        rec.refresh()
        signal_date = target_date or _latest_signal_date(rec)
        if not signal_date:
            return lines + ["status: NO_INPUT", "message: score/factor/price data not found"]
        lines.append(f"signal_date: {signal_date}")
        if not cfg.enabled:
            return lines + ["status: DISABLED"]
        existing = rec.query_optional(
            "SELECT batch_id FROM v_recommendation_batch WHERE signal_date=? AND recommendation_version=? AND COALESCE(run_mode, 'live_shadow')=? LIMIT 1",
            [signal_date, recommendation_version, cfg.run_mode],
        )
        if not existing.empty and not force:
            return lines + [f"skipped_existing_batch: {existing['batch_id'].iloc[0]}"]
        candidates = _build_candidates(rec, cfg, signal_date)
        selected = select_candidates(candidates, cfg, selector_name, {})
        batch_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{signal_date}:{recommendation_version}:{cfg.run_mode}").hex
        batch = _batch_row(batch_id, signal_date, cfg, candidates, selected, selector_name, recommendation_version, cfg.run_mode)
        universe = _universe_rows(batch_id, signal_date, candidates)
        items = _item_rows(batch_id, signal_date, selected, cfg, cfg.run_mode)
        explanations = _explanation_rows(batch_id, selected)
        lines.extend([
            f"universe_count: {len(candidates)}",
            f"eligible_count: {int(candidates.get('included', pd.Series(dtype=bool)).sum()) if not candidates.empty else 0}",
            f"target_top_n: {cfg.top_n}",
            f"selected_count: {len(items)}",
            f"fill_ratio: {batch['fill_ratio']}",
        ])
        if batch["fill_ratio"] < 0.8:
            lines.append(f"WARNING: fill_ratio_below_0.8 selected_count={len(items)} target_top_n={cfg.top_n} reasons={batch['shortfall_reason_json']}")
        if dry_run:
            return lines + ["dry_run: true", f"status: {batch['status']}"]
        rec.write("recommendation_batch", [batch])
        rec.write("recommendation_universe", universe)
        rec.write("recommendation_item", items)
        rec.write("recommendation_explanation", explanations)
        return lines + [f"batch_id: {batch_id}", f"status: {batch['status']}", "saved: recommendation_batch,recommendation_universe,recommendation_item,recommendation_explanation"]
    finally:
        rec.close()


def _latest_signal_date(rec: RecommendationStore) -> str | None:
    for rel in ("v_score_daily", "v_factor_daily", "v_daily_price", "daily_price"):
        value = rec.scalar_optional(f"SELECT MAX(trade_date) FROM {rel}")
        if value:
            return str(pd.to_datetime(value).date())
    return None


def _build_candidates(rec: RecommendationStore, cfg: RecommendationConfig, signal_date: str) -> pd.DataFrame:
    score = _score_rows(rec, signal_date)
    if score.empty:
        return pd.DataFrame()
    out = score.copy()
    out["ts_code"] = out["ts_code"].astype(str)
    out = _merge_basic(rec, out)
    out = _merge_risk(rec, out, signal_date)
    out = _merge_industry(rec, out, signal_date)
    out = _merge_news(rec, out, signal_date)
    out = _merge_price_features(rec, out, signal_date)
    out["risk_level"] = _text_col(out, "risk_level", "MEDIUM").str.upper()
    out["is_st"] = out["name"].map(is_st_name) if "name" in out else False
    out["is_suspended"] = out["is_suspended"].fillna(False) if "is_suspended" in out else False
    out["listing_days"] = _numeric_col(out, "listing_days", cfg.exclude_new_listing_days)
    out["history_days"] = _numeric_col(out, "history_days", 0)
    out["amount_ma20"] = _numeric_col(out, "amount_ma20", 0)
    out["valid_days_20"] = _numeric_col(out, "valid_days_20", 0)
    reasons: list[str] = []
    included: list[bool] = []
    for row in out.to_dict("records"):
        reason = _exclude_reason(row, cfg)
        reasons.append(reason)
        included.append(reason == "")
    out["exclude_reason"] = reasons
    out["included"] = included
    out["total_score_norm"] = normalize_0_1(out.get("total_score", pd.Series(dtype=float)))
    out["risk_adjusted_score"] = out["risk_level"].map(risk_score).fillna(0.5)
    out["industry_strength_score"] = normalize_0_1(out.get("industry_strength_score", pd.Series(dtype=float)))
    if "news_support_score" not in out:
        out["news_support_score"] = 0.5 if cfg.missing_news_policy == "neutral" else 0.0
    weights = _effective_weights(cfg, has_news=bool((pd.to_numeric(out["news_support_score"], errors="coerce").fillna(0) != 0.5).any()))
    out["final_score"] = (
        out["total_score_norm"] * weights["score"]
        + out["risk_adjusted_score"] * weights["risk"]
        + out["industry_strength_score"] * weights["industry"]
        + pd.to_numeric(out["news_support_score"], errors="coerce").fillna(0.5) * weights["news"]
    )
    return out


def _score_rows(rec: RecommendationStore, signal_date: str) -> pd.DataFrame:
    rel = rec.relation("v_score_daily", "score_daily")
    if not rel:
        return _price_score_rows(rec, signal_date)
    df = rec.query_optional(f"SELECT * FROM {rel} WHERE trade_date = DATE '{signal_date}'")
    if df.empty:
        return _price_score_rows(rec, signal_date)
    if "total_score" not in df and "score" in df:
        df["total_score"] = df["score"]
    if "total_score" not in df:
        df["total_score"] = pd.NA
    return df


def _price_score_rows(rec: RecommendationStore, signal_date: str) -> pd.DataFrame:
    rel = rec.relation("v_daily_price", "daily_price")
    if not rel:
        return pd.DataFrame()
    cols = rec.query_optional(f"DESCRIBE SELECT * FROM {rel} LIMIT 0")
    colset = set(cols["column_name"]) if not cols.empty and "column_name" in cols else set()
    amount_expr = "amount" if "amount" in colset else ("volume * close" if {"volume", "close"} <= colset else "0")
    df = rec.query_optional(
        f"""
        WITH p AS (
            SELECT ts_code, trade_date, close, {amount_expr} AS amount
            FROM {rel}
            WHERE trade_date <= DATE '{signal_date}'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date ORDER BY trade_date DESC) = 1
        ),
        latest AS (
            SELECT *
            FROM p
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) = 1
        ),
        hist AS (
            SELECT
                ts_code,
                COUNT(*) AS history_days,
                AVG(amount) FILTER (WHERE trade_date >= DATE '{signal_date}' - INTERVAL 30 DAY) AS amount_ma20,
                MAX(close) FILTER (WHERE trade_date <= DATE '{signal_date}') AS latest_close,
                MIN(close) FILTER (WHERE trade_date >= DATE '{signal_date}' - INTERVAL 90 DAY) AS low_90d
            FROM p
            GROUP BY ts_code
        )
        SELECT
            h.ts_code,
            DATE '{signal_date}' AS trade_date,
            h.history_days,
            h.amount_ma20,
            100.0 * COALESCE((h.latest_close / NULLIF(h.low_90d, 0) - 1), 0)
              + LOG(1 + COALESCE(h.amount_ma20, 0)) AS total_score
        FROM hist AS h
        JOIN latest AS l USING(ts_code)
        """
    )
    if df.empty:
        return df
    df["rank"] = pd.to_numeric(df["total_score"], errors="coerce").rank(ascending=False, method="first").astype(int)
    return df


def _numeric_col(df: pd.DataFrame, column: str, default: float) -> pd.Series:
    if column in df:
        return pd.to_numeric(df[column], errors="coerce").fillna(default)
    return pd.Series([default] * len(df), index=df.index)


def _text_col(df: pd.DataFrame, column: str, default: str) -> pd.Series:
    if column in df:
        return df[column].fillna(default).astype(str)
    return pd.Series([default] * len(df), index=df.index, dtype=str)


def _merge_basic(rec: RecommendationStore, out: pd.DataFrame) -> pd.DataFrame:
    rel = rec.relation("v_stock_basic_latest", "v_stock_basic", "stock_basic")
    if rel:
        basic = rec.query_optional(f"SELECT * FROM {rel}")
        keep = [c for c in ["ts_code", "name", "industry", "list_date"] if c in basic.columns]
        if keep:
            out = out.merge(basic[keep].drop_duplicates("ts_code"), on="ts_code", how="left", suffixes=("", "_basic"))
    if "name" not in out:
        out["name"] = out["ts_code"]
    return out


def _merge_risk(rec: RecommendationStore, out: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    rel = rec.relation("v_risk_flag_daily", "risk_flag_daily")
    if rel:
        risk = rec.query_optional(f"SELECT * FROM {rel} WHERE trade_date = DATE '{signal_date}'")
        keep = [c for c in ["ts_code", "risk_level", "risk_flags", "risk_summary_json"] if c in risk.columns]
        if keep:
            out = out.merge(risk[keep].drop_duplicates("ts_code"), on="ts_code", how="left", suffixes=("", "_risk"))
            if "risk_level_risk" in out:
                out["risk_level"] = out["risk_level_risk"].combine_first(out["risk_level"]) if "risk_level" in out else out["risk_level_risk"]
    return out


def _merge_industry(rec: RecommendationStore, out: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    rel = rec.relation("v_stock_industry_map", "stock_industry_map")
    if rel:
        ind = rec.query_optional(f"SELECT * FROM {rel}")
        keep = [c for c in ["ts_code", "industry_name", "sw_code_2021", "industry_source"] if c in ind.columns]
        if keep:
            out = out.merge(ind[keep].drop_duplicates("ts_code"), on="ts_code", how="left")
    if "industry_name" not in out:
        out["industry_name"] = out.get("industry", "")
    out["industry_missing"] = out["industry_name"].isna() | (out["industry_name"].astype(str).str.strip() == "")
    out["industry_data_source"] = "missing"
    out["industry_warning"] = ""
    board_rel = rec.relation("v_industry_board")
    if board_rel and "industry_name" in out:
        board = rec.query_optional(
            f"""
            SELECT industry_name, industry_code, industry_strength_score, source
            FROM {board_rel}
            WHERE trade_date <= DATE '{signal_date}'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY industry_name ORDER BY trade_date DESC, created_at DESC NULLS LAST) = 1
            """
        )
        if not board.empty:
            out = out.merge(board.drop_duplicates("industry_name"), on="industry_name", how="left", suffixes=("", "_board"))
            if "industry_code_board" in out:
                out["sw_code_2021"] = out["sw_code_2021"].combine_first(out["industry_code_board"]) if "sw_code_2021" in out else out["industry_code_board"]
            out["industry_data_source"] = out["source"].fillna("missing") if "source" in out else "missing"
            out = out.drop(columns=[c for c in ["source", "industry_code_board"] if c in out.columns])
        else:
            out["industry_warning"] = "WARNING: v_industry_board is empty; neutral industry strength used"
    else:
        out["industry_warning"] = "WARNING: v_industry_board missing; neutral industry strength used"
    if "industry_strength_score" not in out:
        out["industry_strength_score"] = pd.NA
    out.loc[out["industry_missing"], "industry_warning"] = "WARNING: industry mapping missing"
    missing_strength = pd.to_numeric(out["industry_strength_score"], errors="coerce").isna()
    out.loc[missing_strength & ~out["industry_missing"] & (out["industry_warning"] == ""), "industry_warning"] = "WARNING: industry strength missing from v_industry_board"
    return out


def _merge_news(rec: RecommendationStore, out: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    rel = rec.relation("v_news_llm_stock_link", "news_llm_stock_link")
    if not rel:
        out["news_support_score"] = 0.5
        return out
    news = rec.query_optional(f"SELECT ts_code, AVG(final_confidence) AS news_support_score FROM {rel} WHERE trade_date = DATE '{signal_date}' GROUP BY ts_code")
    if news.empty:
        out["news_support_score"] = 0.5
        return out
    out = out.merge(news, on="ts_code", how="left")
    out["news_support_score"] = pd.to_numeric(out["news_support_score"], errors="coerce").fillna(0.5)
    return out


def _merge_price_features(rec: RecommendationStore, out: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    rel = rec.relation("v_daily_price", "daily_price")
    if not rel:
        out["history_days"] = 0
        out["amount_ma20"] = 0.0
        out["valid_days_20"] = 0
        return out
    cols = rec.query_optional(f"DESCRIBE SELECT * FROM {rel} LIMIT 0")
    colset = set(cols["column_name"]) if not cols.empty and "column_name" in cols else set()
    amount_expr = "amount" if "amount" in colset else ("volume * close" if {"volume", "close"} <= colset else "0")
    features = rec.query_optional(
        f"""
        WITH p AS (
            SELECT ts_code, trade_date, close, {amount_expr} AS amount
            FROM {rel}
            WHERE trade_date <= DATE '{signal_date}'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date ORDER BY trade_date DESC) = 1
        ),
        agg AS (
            SELECT
                ts_code,
                COUNT(*) AS history_days,
                COUNT(*) FILTER (WHERE trade_date >= DATE '{signal_date}' - INTERVAL 30 DAY) AS valid_days_20,
                AVG(amount) FILTER (WHERE trade_date >= DATE '{signal_date}' - INTERVAL 30 DAY) AS amount_ma20
            FROM p
            GROUP BY ts_code
        )
        SELECT * FROM agg
        """
    )
    if features.empty:
        out["history_days"] = 0
        out["amount_ma20"] = 0.0
        out["valid_days_20"] = 0
        return out
    merged = out.merge(features, on="ts_code", how="left", suffixes=("", "_price"))
    for col in ("history_days", "amount_ma20", "valid_days_20"):
        price_col = f"{col}_price"
        if price_col in merged:
            merged[col] = merged[price_col].combine_first(merged[col]) if col in merged else merged[price_col]
    return merged


def _exclude_reason(row: dict[str, Any], cfg: RecommendationConfig) -> str:
    if cfg.exclude_st and row.get("is_st"):
        return "ST_OR_DELISTING_NAME"
    if cfg.exclude_suspended and row.get("is_suspended"):
        return "SUSPENDED"
    if row.get("history_days", 0) < cfg.min_history_days:
        return "INSUFFICIENT_HISTORY"
    if row.get("valid_days_20", 0) < cfg.min_valid_days_20:
        return "INSUFFICIENT_RECENT_TRADING_DAYS"
    if row.get("amount_ma20", 0) < cfg.min_amount_ma20:
        return "LOW_LIQUIDITY"
    if row.get("listing_days", cfg.exclude_new_listing_days) < cfg.exclude_new_listing_days:
        return "NEW_LISTING"
    if str(row.get("risk_level", "MEDIUM")).upper() not in cfg.allowed_risk_levels:
        return "RISK_LEVEL_EXCLUDED"
    if cfg.require_news_signal and float(row.get("news_support_score") or 0) <= 0.5:
        return "MISSING_NEWS_SIGNAL"
    return ""


def _effective_weights(cfg: RecommendationConfig, has_news: bool) -> dict[str, float]:
    weights = {"score": cfg.score_weight, "risk": cfg.risk_weight, "industry": cfg.industry_strength_weight, "news": cfg.news_weight if has_news else 0.0}
    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}


def select_candidates(candidates: pd.DataFrame, cfg: RecommendationConfig, selector_name: str = "raw_top_n", state: dict[str, Any] | None = None) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    eligible = candidates[candidates["included"]].sort_values(["final_score", "total_score"], ascending=False).copy()
    if eligible.empty:
        return eligible
    eligible["raw_rank"] = range(1, len(eligible) + 1)
    state = state if state is not None else {}
    if selector_name == "confirmed_top_n":
        eligible = _confirmed_candidates(eligible, cfg, state)
    elif selector_name == "low_turnover_top_n":
        eligible = _low_turnover_candidates(eligible, cfg, state)
    selected = _apply_industry_cap(eligible, cfg)
    state["selected_set"] = set(selected["ts_code"].astype(str).tolist()) if not selected.empty else set()
    return selected


def _apply_industry_cap(eligible: pd.DataFrame, cfg: RecommendationConfig) -> pd.DataFrame:
    selected = []
    counts: dict[str, int] = {}
    for row in eligible.to_dict("records"):
        industry = str(row.get("industry_name") or row.get("industry") or "")
        if industry and counts.get(industry, 0) >= cfg.max_industry_count:
            continue
        counts[industry] = counts.get(industry, 0) + 1
        selected.append(row)
        if len(selected) >= cfg.top_n:
            break
    return pd.DataFrame(selected)


def _confirmed_candidates(eligible: pd.DataFrame, cfg: RecommendationConfig, state: dict[str, Any]) -> pd.DataFrame:
    streaks = state.setdefault("entry_streak", {})
    exit_streaks = state.setdefault("exit_streak", {})
    previous = set(state.get("selected_set", set()))
    ranks = dict(zip(eligible["ts_code"].astype(str), eligible["raw_rank"]))
    risk = dict(zip(eligible["ts_code"].astype(str), eligible["risk_level"].astype(str).str.upper()))
    for code in eligible["ts_code"].astype(str):
        rank = int(ranks[code])
        streaks[code] = streaks.get(code, 0) + 1 if rank <= cfg.confirmed_entry_top_n else 0
        exit_streaks[code] = exit_streaks.get(code, 0) + 1 if rank > cfg.confirmed_exit_top_n else 0
    keep = {code for code in previous if code in ranks and risk.get(code) != "HIGH" and exit_streaks.get(code, 0) < cfg.confirmed_exit_days}
    add = {code for code in ranks if streaks.get(code, 0) >= cfg.confirmed_entry_days and risk.get(code) != "HIGH"}
    selected_codes = keep | add
    out = eligible[eligible["ts_code"].astype(str).isin(selected_codes)].copy()
    return out.sort_values(["final_score", "total_score"], ascending=False)


def _low_turnover_candidates(eligible: pd.DataFrame, cfg: RecommendationConfig, state: dict[str, Any]) -> pd.DataFrame:
    previous = set(state.get("selected_set", set()))
    if not previous:
        return eligible
    previous_rows = eligible[eligible["ts_code"].astype(str).isin(previous) & (eligible["risk_level"].astype(str).str.upper() != "HIGH")].copy()
    previous_rows = previous_rows.sort_values(["final_score", "total_score"], ascending=False).head(cfg.top_n)
    min_kept = float(previous_rows["final_score"].min()) if not previous_rows.empty else -1.0
    challengers = eligible[~eligible["ts_code"].astype(str).isin(set(previous_rows["ts_code"].astype(str)))].copy()
    challengers = challengers[challengers["final_score"] > min_kept + cfg.replacement_score_gap]
    out = pd.concat([previous_rows, challengers], ignore_index=True).sort_values(["final_score", "total_score"], ascending=False)
    return out.head(cfg.top_n)


def _effective_version(cfg: RecommendationConfig, selector_name: str) -> str:
    return cfg.recommendation_version if selector_name == "raw_top_n" else f"{cfg.recommendation_version}:{selector_name}"


def _batch_row(batch_id: str, signal_date: str, cfg: RecommendationConfig, candidates: pd.DataFrame, selected: pd.DataFrame, selector_name: str, recommendation_version: str, run_mode: str | None = None) -> dict:
    eligible_count = int(candidates["included"].sum()) if not candidates.empty and "included" in candidates else 0
    status = "WAITING_ENTRY" if len(selected) else "FAILED"
    selected_count = len(selected)
    target_top_n = int(cfg.top_n)
    fill_ratio = selected_count / target_top_n if target_top_n else None
    shortfall_reasons = _shortfall_reasons(candidates, selected_count, target_top_n)
    return {
        "batch_id": batch_id,
        "trade_date": trade_date(signal_date),
        "signal_date": trade_date(signal_date),
        "recommendation_version": recommendation_version,
        "selector_name": selector_name,
        "run_mode": run_mode or cfg.run_mode,
        "factor_version": "latest",
        "score_version": "latest",
        "risk_version": "latest",
        "news_analysis_version": "latest",
        "top_n": cfg.top_n,
        "target_top_n": target_top_n,
        "holding_period_days": cfg.holding_period_days,
        "entry_price_type": cfg.entry_price_type,
        "weighting_method": cfg.weighting,
        "benchmark_index": cfg.benchmark_index,
        "universe_count": len(candidates),
        "eligible_count": eligible_count,
        "selected_count": selected_count,
        "fill_ratio": fill_ratio,
        "shortfall_reason_json": json.dumps(shortfall_reasons, ensure_ascii=False, sort_keys=True),
        "quality_status": "WARNING" if fill_ratio is not None and fill_ratio < 0.8 else "OK",
        "market_regime": "",
        "config_json": json.dumps(cfg.raw or _config_dict(cfg), ensure_ascii=False, default=str, sort_keys=True),
        "status": status,
        "created_at": pd.Timestamp.now(),
        "updated_at": pd.Timestamp.now(),
    }


def _universe_rows(batch_id: str, signal_date: str, candidates: pd.DataFrame) -> list[dict]:
    rows = []
    for row in candidates.to_dict("records"):
        rows.append({
            "batch_id": batch_id,
            "trade_date": trade_date(signal_date),
            "signal_date": trade_date(signal_date),
            "ts_code": row.get("ts_code"),
            "included": bool(row.get("included")),
            "exclude_reason": row.get("exclude_reason", ""),
            "history_days": _safe_int(row.get("history_days"), 0),
            "amount_ma20": _safe_float(row.get("amount_ma20"), 0.0),
            "risk_level": row.get("risk_level", ""),
            "is_st": bool(row.get("is_st")),
            "is_suspended": bool(row.get("is_suspended")),
            "listing_days": _safe_int(row.get("listing_days"), 0),
            "created_at": pd.Timestamp.now(),
        })
    return rows


def _item_rows(batch_id: str, signal_date: str, selected: pd.DataFrame, cfg: RecommendationConfig, run_mode: str | None = None) -> list[dict]:
    if selected.empty:
        return []
    rows = []
    weight = 1.0 / len(selected) if cfg.weighting == "equal" and len(selected) else 0.0
    ranked = selected.sort_values("final_score", ascending=False).reset_index(drop=True)
    for idx, row in ranked.iterrows():
        rows.append({
            "batch_id": batch_id,
            "trade_date": trade_date(signal_date),
            "signal_date": trade_date(signal_date),
            "run_mode": run_mode or cfg.run_mode,
            "ts_code": row.get("ts_code"),
            "name": row.get("name", row.get("ts_code")),
            "industry_code": row.get("sw_code_2021", ""),
            "industry_name": row.get("industry_name", row.get("industry", "")),
            "rank": int(idx + 1),
            "final_score": _safe_float(row.get("final_score"), 0.0),
            "total_score": _safe_float(row.get("total_score"), 0.0),
            "risk_level": row.get("risk_level", ""),
            "risk_flags": row.get("risk_flags", ""),
            "industry_strength_score": _safe_float(row.get("industry_strength_score"), 0.0),
            "industry_data_source": row.get("industry_data_source", "missing"),
            "industry_missing": bool(row.get("industry_missing", False)),
            "industry_warning": row.get("industry_warning", ""),
            "news_support_score": _safe_float(row.get("news_support_score"), 0.0),
            "selection_reason_json": json.dumps({"score": row.get("total_score"), "risk_level": row.get("risk_level"), "industry_data_source": row.get("industry_data_source", "missing"), "industry_warning": row.get("industry_warning", ""), "news_support_score": row.get("news_support_score")}, ensure_ascii=False, default=str),
            "risk_summary_json": row.get("risk_summary_json", "[]"),
            "weight": weight,
            "entry_date": None,
            "entry_price": None,
            "tracking_status": "WAITING_ENTRY",
            "created_at": pd.Timestamp.now(),
            "updated_at": pd.Timestamp.now(),
        })
    return rows


def _shortfall_reasons(candidates: pd.DataFrame, selected_count: int, target_top_n: int) -> dict[str, int | str]:
    if selected_count >= target_top_n:
        return {}
    if candidates.empty:
        return {"NO_CANDIDATES": target_top_n - selected_count}
    if "exclude_reason" not in candidates:
        return {"UNKNOWN_SHORTFALL": target_top_n - selected_count}
    excluded = candidates[~candidates.get("included", pd.Series(dtype=bool)).astype(bool)]
    counts = excluded["exclude_reason"].fillna("UNKNOWN").replace("", "UNKNOWN").value_counts().to_dict()
    capped = {str(k): int(v) for k, v in counts.items()}
    if selected_count:
        capped["SELECTED_BELOW_TARGET"] = int(target_top_n - selected_count)
    return capped or {"INSUFFICIENT_ELIGIBLE_AFTER_FILTERS": int(target_top_n - selected_count)}


def _config_dict(cfg: RecommendationConfig) -> dict:
    try:
        return asdict(cfg)
    except TypeError:
        return dict(getattr(cfg, "__dict__", {}))


def _safe_float(value: Any, default: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(value) else value


def _safe_int(value: Any, default: int) -> int:
    return int(_safe_float(value, float(default)))


def _explanation_rows(batch_id: str, selected: pd.DataFrame) -> list[dict]:
    rows = []
    for row in selected.to_dict("records"):
        rows.append({
            "batch_id": batch_id,
            "trade_date": trade_date(row.get("trade_date") or row.get("signal_date") or pd.Timestamp.now()),
            "ts_code": row.get("ts_code"),
            "explanation_version": "rule_v1",
            "model_name": "rule_based",
            "factor_summary": f"综合评分分位 {row.get('total_score_norm', 0):.2f}",
            "industry_summary": f"行业强度 {row.get('industry_strength_score', 0):.2f} 来源 {row.get('industry_data_source', 'missing')}",
            "news_summary": "新闻仅作为辅助信号，未直接决定名单。",
            "risk_summary": f"风险等级 {row.get('risk_level', '')}",
            "uncertainty": "观察池为研究信号和模拟跟踪，不构成交易建议。",
            "explanation_markdown": "基于因子评分、风险约束、行业分散和新闻辅助信号生成的模拟观察项。",
            "created_at": pd.Timestamp.now(),
        })
    return rows
