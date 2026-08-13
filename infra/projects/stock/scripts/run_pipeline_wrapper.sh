#!/usr/bin/env bash
set -euo pipefail
export TZ=Asia/Shanghai

# Trap SIGTERM and just exit - but DO print to log so we know
trap 'echo "[run_pipeline] Caught SIGTERM at $(date)" >> /mnt/data/stock/logs/daily_run.log; exit 143' TERM

cd /mnt/data/stock/stackAnalys

# Run the pipeline
echo "[run_pipeline] Starting at $(date)" >> /mnt/data/stock/logs/daily_run.log
/mnt/data/stock/stackAnalys/.venv/bin/python scripts/run_daily.py --db-path /mnt/data/stock/stock_local_ai_data/stock_data_v2.duckdb
RET=$?
echo "[run_pipeline] Exited with $RET at $(date)" >> /mnt/data/stock/logs/daily_run.log
exit $RET
