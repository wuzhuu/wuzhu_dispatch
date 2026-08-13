#!/usr/bin/env bash
# 周末/休息日 RSS 新闻采集
# 仅采集新闻，不做股票行情数据采集
# 由 crontab 触发（周六/周日 7:00 和 19:00）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/weekend_rss_news.log"
LOCAL_DB="$ROOT_DIR/stock_local_ai_data/stock_data_v2.duckdb"

mkdir -p "$LOG_DIR"
log_date=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$log_date] === RSS 新闻采集（休息日）开始 ===" >> "$LOG_FILE"

cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"

set +e
"$VENV_DIR/bin/python" -c "
from src.collectors.rss_news_collector import run_ingest
result = run_ingest()
new = int(result.get('new_rows', 0))
sources = int(result.get('fetched_sources', 0))
failed = len(result.get('failed', []))
skipped = int(result.get('skipped_sources', 0))
print(f'new_rows={new} sources={sources} failed={failed} skipped={skipped}')
for s in result.get('samples', []):
    print(f'  sample: [{s.get(\"source\",\"?\")}] {s.get(\"title\",\"\")[:60]}')
for f in result.get('failed', []):
    print(f'  FAILED: {f.get(\"source_id\")}: {f.get(\"error\",\"\")[:80]}')
" >> "$LOG_FILE" 2>&1
RET=$?
set -e

echo "[$log_date] 退出码: $RET" >> "$LOG_FILE"
echo "[$log_date] === RSS 新闻采集（休息日）结束 ===" >> "$LOG_FILE"
exit $RET
