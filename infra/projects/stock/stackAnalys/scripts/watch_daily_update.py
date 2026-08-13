from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.daily_watcher import run_daily_watcher
from src.utils.config import add_db_path_arg, load_settings
from src.utils.proxy import disable_proxy_if_configured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch free data sources and run one low-frequency daily increment when ready.")
    add_db_path_arg(parser)
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD; default today.")
    return parser.parse_args()


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    if not settings.get("watcher", {}).get("enabled", True):
        print("daily watcher is disabled in config/settings.yaml")
        return
    decision = run_daily_watcher(target_date=args.date, db_path=args.db_path)
    print(f"{decision.status}: {decision.message}")


if __name__ == "__main__":
    main()
