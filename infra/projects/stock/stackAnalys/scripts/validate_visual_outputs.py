from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.maintenance.common import check_row, dataset_relation, print_rows, resolve_data_root, resolve_db_path, write_maintenance_rows
from src.utils.config import resolve_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate visualization outputs and tracked warning states.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db_path)
    data_root = resolve_data_root(db_path)
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = validate_visual(conn, data_root)
    finally:
        conn.close()
    print_rows(rows)
    if not args.no_persist:
        write_maintenance_rows(db_path, "visualization_validation", rows)
        print("saved: lake/visualization_validation")


def validate_visual(conn: duckdb.DuckDBPyConnection, data_root: Path) -> list[dict]:
    rows: list[dict] = []
    snap_rel = dataset_relation(conn, data_root, "visualization_snapshot")
    metric_rel = dataset_relation(conn, data_root, "visualization_metric")
    rows.append(check_row("visualization_snapshot_exists", "OK" if snap_rel else "FAIL", str(snap_rel), 1 if snap_rel else 0, 1))
    rows.append(check_row("visualization_metric_exists", "OK" if metric_rel else "FAIL", str(metric_rel), 1 if metric_rel else 0, 1))
    if snap_rel:
        df = conn.execute(f"SELECT * FROM {snap_rel}").df()
        rows.append(check_row("visualization_snapshot_rows", "OK" if len(df) else "FAIL", f"rows={len(df)}", len(df), 1))
        missing_png = 0
        warnings = 0
        for _, row in df.iterrows():
            image_path = str(row.get("image_path") or "")
            payload = {}
            try:
                payload = json.loads(row.get("summary_json") or "{}")
            except Exception:
                pass
            if payload.get("status") == "WARNING":
                warnings += 1
                continue
            if image_path and not resolve_path(image_path).exists():
                missing_png += 1
        rows.append(check_row("visualization_missing_png", "OK" if missing_png == 0 else "FAIL", f"missing_png={missing_png}", missing_png, 0))
        rows.append(check_row("visualization_warning_states", "OK", f"warnings={warnings}", warnings, None))
    if metric_rel:
        count = conn.execute(f"SELECT COUNT(*) FROM {metric_rel}").fetchone()[0]
        rows.append(check_row("visualization_metric_rows", "OK" if count else "FAIL", f"rows={count}", count, 1))
    project_outputs = []
    for rel in ("data/reports", "reports", "data/samples"):
        root = PROJECT_ROOT / rel
        if root.exists():
            project_outputs.extend([p for p in root.rglob("*") if p.is_file()])
    rows.append(check_row("project_visual_outputs", "OK" if not project_outputs else "FAIL", f"files={len(project_outputs)}", len(project_outputs), 0))
    return rows


if __name__ == "__main__":
    main()
