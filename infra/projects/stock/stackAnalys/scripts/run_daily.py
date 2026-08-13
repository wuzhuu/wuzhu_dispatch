from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.daily_update_job import run_daily_update
from src.utils.config import add_db_path_arg
from src.utils.proxy import disable_proxy_if_configured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the daily update job.")
    add_db_path_arg(parser)
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD; default today.")
    parser.add_argument("--batch-size", type=int, default=None, help="Sina spot batch size; recommended 50-100.")
    parser.add_argument("--sina-qps", type=float, default=None, help="Sina host-level QPS limit; default from settings.")
    parser.add_argument("--max-concurrency", type=int, default=None, help="Max concurrent Sina batch requests.")
    parser.add_argument("--timeout", type=float, default=None, help="Sina request timeout seconds.")
    parser.add_argument("--retries", type=int, default=None, help="Sina retry attempts.")
    parser.add_argument("--write-temp-before-close", action="store_true", help="Write valid Sina spot rows to daily_price_temp before formal close.")
    parser.add_argument("--repair-missing", action="store_true", help="Repair pending missing_daily_price rows with historical providers after collection.")
    parser.add_argument("--no-resume", action="store_true", help="Disable Sina batch checkpoint resume and refetch all batches.")
    parser.add_argument("--dry-run", action="store_true", help="Collect and report without writing price rows or status tables.")
    return parser.parse_args()


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    collector_overrides = {
        "sina_batch_size": args.batch_size,
        "sina_qps": args.sina_qps,
        "sina_max_concurrency": args.max_concurrency,
        "sina_timeout": args.timeout,
        "sina_retries": args.retries,
        "sina_resume_enabled": False if args.no_resume else None,
    }
    if args.write_temp_before_close:
        collector_overrides["sina_write_temp_before_close"] = True
    run_daily_update(
        args.date,
        db_path=args.db_path,
        dry_run=args.dry_run,
        repair_missing=args.repair_missing,
        settings_overrides={"collector": collector_overrides},
    )


if __name__ == "__main__":
    main()
