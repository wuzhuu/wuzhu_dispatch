#!/usr/bin/env bash
# 每日 22:05 将 stock 工程文件同步到 wuzhunas NAS (SMB) nasdisk 共享
# 使用 rclone（~/.local/bin/rclone），配置在 ~/.config/rclone/rclone.conf
# 首次同步约 10-15 分钟（~2.3GB），后续增量秒级

set -euo pipefail

PROJECT_DIR="$HOME/stock"
RCLONE="$HOME/.local/bin/rclone"
REMOTE="wuzhunas:nasdisk/stock"
LOG_FILE="$PROJECT_DIR/logs/sync_to_nasdisk.log"
LOG_DATE_FORMAT='+%Y-%m-%d %H:%M:%S'

log() {
    echo "[$(date "$LOG_DATE_FORMAT")] $1" >> "$LOG_FILE"
}

log "=== 开始同步 stock → wuzhunas:nasdisk/stock ==="

if [ ! -f "$RCLONE" ]; then
    log "❌ rclone 不存在: $RCLONE"
    exit 1
fi

cd "$PROJECT_DIR"

# 同步整个项目目录
# 排除: .git (版本控制), __pycache__, .venv, .duckdb.wal/lock (运行时锁文件)
$RCLONE sync "$PROJECT_DIR/" "$REMOTE" \
    --exclude ".git/**" \
    --exclude "**/__pycache__/**" \
    --exclude "stackAnalys/.venv/**" \
    --exclude "**/*.duckdb.wal" \
    --exclude "**/*.duckdb.lock" \
    --verbose \
    2>&1 >> "$LOG_FILE"

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    log "✅ 同步完成"
else
    log "❌ 同步失败 (exit=$EXIT_CODE)"
fi

log "=== 同步结束 ==="
echo ""
exit $EXIT_CODE
