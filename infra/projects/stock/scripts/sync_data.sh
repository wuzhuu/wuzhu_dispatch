#!/usr/bin/env bash
# 数据同步脚本 — 同步整个 stock_local_ai_data/ 目录
# NAS: 单向同步 + 保留 3 天的版本快照
# SD卡: 本地SSD ↔ SD卡双向同步 (Unison)
# 用法:
#   ./sync_data.sh              # 完整同步 (NAS + SD卡)
#   ./sync_data.sh --nas-only   # 仅 NAS 单向同步 + 版本快照
#   ./sync_data.sh --sd-only    # 仅 SD 卡双向同步
#   ./sync_data.sh --cleanup    # 仅清理 NAS 过期快照

set -euo pipefail

# ========== 路径 ==========
LOCAL_DIR="$HOME/stock/stock_local_ai_data"

# NAS
NAS_BASE="/mnt/nasdisk/stock_local_ai_data"
NAS_BACKUP_DIR="$NAS_BASE/backups"
NAS_RETENTION_DAYS=3

# rclone（SMB 比 rsync 稳定：rsync 在 CIFS 挂载上会卡死）
RCLONE="$HOME/.local/bin/rclone"
RCLONE_REMOTE="wuzhunas:nasdisk/stock_local_ai_data"

# SD 卡 (Unison 自己管理路径，这里只需检查挂载)
SD_MOUNT="/mnt/sdcard"

LOG_FILE="$HOME/stock/logs/sync.log"
DATE_TAG=$(date '+%Y-%m-%d')
export TZ=Asia/Shanghai
log_date=$(date '+%Y-%m-%d %H:%M:%S')

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# ========== 检查挂载 ==========
check_mount() {
    local mount_point="$1"
    local label="$2"
    if mountpoint -q "$mount_point" 2>/dev/null; then
        return 0
    else
        log "WARNING: $label 挂载点 $mount_point 不可用，跳过"
        return 1
    fi
}

total_size() {
    du -sh "$1" 2>/dev/null | cut -f1 || echo "?"
}

# ========== NAS 单向同步 + 版本快照 ==========
sync_nas() {
    log "--- NAS 同步开始 ---"

    if ! check_mount "/mnt/nasdisk" "NAS"; then
        return 1
    fi

    # 1. 创建日期版本快照 (rclone sync，SMB稳定；保留 3 天)
    local snapshot_dir="$NAS_BACKUP_DIR/$DATE_TAG"
    log "创建版本快照: $snapshot_dir"
    timeout 3600 "$RCLONE" sync "$LOCAL_DIR/" "$RCLONE_REMOTE/backups/$DATE_TAG" \
        --transfers 4 --fast-list --retries 3 --low-level-retries 10 2>&1 >> "$LOG_FILE" && \
        log "  快照完成 (NAS 上 du 太慢，不统计大小)" || \
        log "  WARNING: 快照 rclone 异常或超时"

    # 2. 更新 NAS 实时副本 (排除快照目录，防止 delete 误删)
    log "更新 NAS 实时副本..."
    timeout 3600 "$RCLONE" sync "$LOCAL_DIR/" "$RCLONE_REMOTE" \
        --transfers 4 --fast-list --exclude "/backups/**" --retries 3 --low-level-retries 10 2>&1 >> "$LOG_FILE" && \
        log "  实时副本完成" || \
        log "  WARNING: 实时副本 rclone 异常或超时"

    # 3. 清理过期快照 (保留最近 3 天)
    log "清理 $NAS_RETENTION_DAYS 天前的快照..."
    local cleaned=0
    while IFS= read -r -d '' dir; do
        local dirname
        dirname=$(basename "$dir")
        if [[ "$dirname" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
            local dir_epoch
            dir_epoch=$(date -d "$dirname" +%s 2>/dev/null || echo 0)
            local cutoff_epoch
            cutoff_epoch=$(date -d "$DATE_TAG - $NAS_RETENTION_DAYS days" +%s)
            if [ "$dir_epoch" -gt 0 ] && [ "$dir_epoch" -lt "$cutoff_epoch" ]; then
                rm -rf "$dir"
                log "  删除过期快照: $dirname"
                cleaned=$((cleaned + 1))
            fi
        fi
    done < <(find "$NAS_BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)
    log "NAS 同步完成，清理了 $cleaned 个过期快照"
    return 0
}

# ========== SD 卡双向同步 (Unison) ==========
sync_sd() {
    log "--- SD 卡双向同步开始 (Unison) ---"

    if ! check_mount "$SD_MOUNT" "SD卡"; then
        return 1
    fi

    mkdir -p "$LOCAL_DIR"
    if unison ssd-sdcard -batch -terse 2>&1; then
        log "  Unison 同步成功"
    else
        local ret=$?
        log "  WARNING: Unison 退出码 $ret"
    fi

    log "SD 卡双向同步完成"
    return 0
}

# ========== 主流程 ==========
case "${1:-all}" in
    --nas-only)
        sync_nas
        ;;
    --sd-only)
        sync_sd
        ;;
    --cleanup)
        log "--- 仅清理 NAS 过期快照 ---"
        if check_mount "/mnt/nasdisk" "NAS"; then
            cleaned=0
            while IFS= read -r -d '' dir; do
                dirname=$(basename "$dir")
                if [[ "$dirname" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
                    dir_epoch=$(date -d "$dirname" +%s 2>/dev/null || echo 0)
                    cutoff_epoch=$(date -d "$DATE_TAG - $NAS_RETENTION_DAYS days" +%s)
                    if [ "$dir_epoch" -gt 0 ] && [ "$dir_epoch" -lt "$cutoff_epoch" ]; then
                        rm -rf "$dir"
                        cleaned=$((cleaned + 1))
                    fi
                fi
            done < <(find "$NAS_BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)
            log "清理完成，删除了 $cleaned 个过期快照"
        fi
        ;;
    all|*)
        sync_nas
        sync_sd
        ;;
esac

log "=== sync_data.sh 结束 ==="
echo "" >> "$LOG_FILE"
