from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.daily_update_job import run_daily_update
from src.utils.config import add_db_path_arg, ensure_project_dirs, load_settings
from src.utils.logger import setup_logger
from src.utils.proxy import disable_proxy_if_configured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduler that runs the daily update job on a cron schedule.")
    add_db_path_arg(parser)
    return parser.parse_args()


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    ensure_project_dirs(settings)
    log = setup_logger("scheduler.log")
    hour, minute = settings["schedule"]["daily_update_time"].split(":")
    timezone = settings["project"]["timezone"]

    def job_wrapper():
        run_daily_update(db_path=args.db_path)

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(job_wrapper, CronTrigger(hour=int(hour), minute=int(minute), timezone=timezone), id="daily_update", name="daily_update", max_instances=1, coalesce=True, replace_existing=True)
    log.info("Scheduler started. daily_update runs every day at {} {}", settings["schedule"]["daily_update_time"], timezone)
    scheduler.start()


if __name__ == "__main__":
    main()
