from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from .config import load_recommendation_config
from .evaluator import finalize_results
from .selector import (
    _batch_row,
    _build_candidates,
    _effective_version,
    _explanation_rows,
    _item_rows,
    _universe_rows,
    select_candidates,
)
from .stores import RecommendationStore
from .tracker import update_tracking


def backfill_recommendation_history(
    db_path: str | Path | None,
    config_path: str | Path | None,
    start: str,
    end: str,
    force: bool = False,
    dry_run: bool = False,
    selectors: list[str] | None = None,
) -> list[str]:
    cfg = load_recommendation_config(config_path)
    selector_names = selectors or list(cfg.selector_names)
    rec = RecommendationStore(db_path)
    lines = [f"start: {start}", f"end: {end}", f"selectors: {','.join(selector_names)}"]
    states: dict[str, dict[str, Any]] = {name: {} for name in selector_names}
    generated = skipped = failed = 0
    try:
        rec.refresh()
        dates = _trade_dates(rec, start, end)
        if not dates:
            return lines + ["status: NO_PRICE_DATA"]
        for signal_date in dates:
            candidates = _build_candidates(rec, cfg, signal_date)
            if candidates.empty:
                failed += len(selector_names)
                continue
            for selector_name in selector_names:
                version = _effective_version(cfg, selector_name)
                if not force and _existing_batch(rec, signal_date, version, "backfill"):
                    skipped += 1
                    continue
                selected = select_candidates(candidates.copy(), cfg, selector_name, states[selector_name])
                batch_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{signal_date}:{version}:backfill").hex
                batch = _batch_row(batch_id, signal_date, cfg, candidates, selected, selector_name, version, "backfill")
                universe = _universe_rows(batch_id, signal_date, candidates)
                items = _item_rows(batch_id, signal_date, selected, cfg, "backfill")
                explanations = _explanation_rows(batch_id, selected)
                generated += 1
                if dry_run:
                    continue
                rec.write("recommendation_batch", [batch])
                rec.write("recommendation_universe", universe)
                rec.write("recommendation_item", items)
                rec.write("recommendation_explanation", explanations)
        lines.extend([f"trade_dates: {len(dates)}", f"generated_batches: {generated}", f"skipped_existing_batches: {skipped}", f"failed_candidate_days: {failed}"])
    finally:
        rec.close()
    if dry_run:
        return lines + ["dry_run: true"]
    track_lines = update_tracking(db_path, config_path, target_date=None, batch_id=None, dry_run=False, force=force)
    result_lines = finalize_results(db_path, config_path, batch_id=None, dry_run=False)
    return lines + track_lines + result_lines + ["status: DONE"]


def _trade_dates(rec: RecommendationStore, start: str, end: str) -> list[str]:
    rel = rec.relation("v_daily_price", "daily_price")
    if not rel:
        rel = rec.relation("v_score_daily", "score_daily")
    if not rel:
        return []
    df = rec.query_optional(
        f"""
        SELECT DISTINCT trade_date
        FROM {rel}
        WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}'
        ORDER BY trade_date
        """
    )
    if df.empty:
        return []
    return [str(pd.to_datetime(x).date()) for x in df["trade_date"].dropna().tolist()]


def _existing_batch(rec: RecommendationStore, signal_date: str, version: str, run_mode: str) -> bool:
    df = rec.query_optional(
        "SELECT 1 FROM v_recommendation_batch WHERE signal_date=? AND recommendation_version=? AND COALESCE(run_mode, 'live_shadow')=? LIMIT 1",
        [signal_date, version, run_mode],
    )
    return not df.empty
