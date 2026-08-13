#!/usr/bin/env bash
# 每日数据采集完成后，自动推送数据到 GitHub
# 定时任务: 每日 22:00 (交易日)，在 run_daily 和 sync_data 之后执行
# 推送到主仓库：scripts/ logs/ stock_local_ai_data/（不含 lake/ parquet）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$ROOT_DIR/logs/git_push_data.log"
export TZ=Asia/Shanghai

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"; }

log "=== 数据 GitHub 推送开始 ==="

cd "$ROOT_DIR"

# 检查是否有变更（排除 lake/）
git add -A
if git diff --cached --quiet; then
    log "无变更，跳过推送"
    exit 0
fi

COMMIT_MSG="data update: $(date +%Y-%m-%d %H:%M)"
git commit -m "$COMMIT_MSG" >> "$LOG_FILE" 2>&1 || { log "commit 无变化"; exit 0; }

# 推送（直连 GitHub，失败时尝试通过代理）
log "正在推送到 GitHub..."
if git push origin main >> "$LOG_FILE" 2>&1; then
    log "推送成功"
else
    log "直连推送失败，尝试代理推送..."
    export http_proxy="http://gql:7892"
    export https_proxy="http://gql:7892"
    if git push origin main >> "$LOG_FILE" 2>&1; then
        log "代理推送成功"
    else
        log "ERROR: 推送仍然失败，请手动处理"
    fi
    unset http_proxy https_proxy
fi

log "推送完成"
echo "" >> "$LOG_FILE"
