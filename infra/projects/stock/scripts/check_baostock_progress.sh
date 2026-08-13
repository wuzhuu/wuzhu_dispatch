#!/bin/bash
# Check BaoStock daily update progress
# Used by cron job to report status to user
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
LOG_FILE="$ROOT_DIR/logs/daily_update.log"
PID_FILE="$ROOT_DIR/logs/daily_run.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
else
    PID=$(pgrep -fo '(^|[ /])run_daily\.py([ ]|$)' 2>/dev/null || true)
fi

if [ ! -f "$LOG_FILE" ]; then
    echo "❌ 日志文件不存在: $LOG_FILE"
    exit 0
fi

# Check if process is running
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    STATUS="🟢 运行中"
    UPTIME=$(ps -o etime= -p "$PID" 2>/dev/null | xargs)
else
    STATUS="🔴 已结束"
    UPTIME=""
fi

# Get last processed stock code
LAST_CODE=$(grep -oE 'daily_price:(sz|sh|bj)\.[0-9]+' "$LOG_FILE" | sed 's/^daily_price://' | tail -1)
TOTAL_LINES=$(wc -l < "$LOG_FILE")
SUCCESS_LINES=$(grep -c "primary succeeded" "$LOG_FILE" 2>/dev/null || echo 0)
FAIL_LINES=$(grep -c "FAIL" "$LOG_FILE" 2>/dev/null || echo 0)
SKIP_LINES=$(grep -c "SKIP" "$LOG_FILE" 2>/dev/null || echo 0)

# Get start time from first log entry
START_TIME=$(head -1 "$LOG_FILE" | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' || echo "unknown")

# Get last 3 entries
echo ""
echo "========================================"
echo " BaoStock 每日更新监控报告"
echo "========================================"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "状态: $STATUS"
echo "运行时间: $UPTIME"
echo "进程 PID: ${PID:-未发现}"
echo "开始时间: $START_TIME"
echo "日志行数: $TOTAL_LINES"
echo "成功行数: $SUCCESS_LINES"
echo "失败行数: $FAIL_LINES"
echo "跳过行数: $SKIP_LINES"
echo "最后处理: ${LAST_CODE:-未知}"
echo "----------------------------------------"
echo "最近日志:"
tail -3 "$LOG_FILE"
echo "========================================"
