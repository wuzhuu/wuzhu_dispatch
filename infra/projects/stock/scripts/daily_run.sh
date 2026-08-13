#!/usr/bin/env bash
# 每日数据采集（使用智能监视器）+ 数据库同步到 NAS
# 15:05 后东方财富全市场快照补当天日线，备用窗口 18:30/19:30/21:00
# 自动检测本地数据库；缺失时从 NAS 恢复

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
if [ ! -d "$VENV_DIR" ] && [ -d "$PROJECT_DIR/.venv" ]; then
    VENV_DIR="$PROJECT_DIR/.venv"
fi
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_run.log"
PID_FILE="$LOG_DIR/daily_run.pid"
LOCAL_DIR="$ROOT_DIR/stock_local_ai_data"
LOCAL_DB="$LOCAL_DIR/stock_data_v2.duckdb"
NAS_DIR="/mnt/nasdisk/stock_local_ai_data"
RECOMMENDATION_CONFIG="${RECOMMENDATION_CONFIG:-$HOME/key/recommendation.yaml}"
SELF_MAX_RUNTIME="${SELF_MAX_RUNTIME:-28800}"

export TZ=Asia/Shanghai
log_date=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$LOG_DIR"

# ---- 目标交易日：凌晨运行时回退到上一交易日 ----
# 凌晨 2 点跑时 `date` 是"今天"（未开盘），行情目标日期应为上一交易日。
TARGET_DATE=$(timeout 30 "$VENV_DIR/bin/python" "$ROOT_DIR/scripts/get_target_date.py" 2>/dev/null || date -d "yesterday" '+%Y-%m-%d')
echo "[$log_date] 目标交易日: $TARGET_DATE" >> "$LOG_FILE" 2>&1
export TARGET_DATE

# ---- 防重入检查：已有实例运行则退出 ----
if [ -f "$PID_FILE" ]; then
    old_pid=$(cat "$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
        echo "[$log_date] WARNING: 已有实例 PID=$old_pid 在运行，跳过本次启动" >> "$LOG_FILE"
        exit 0
    else
        echo "[$log_date] 发现残留 PID_FILE（PID=$old_pid 已不在运行），清理后继续" >> "$LOG_FILE"
        rm -f "$PID_FILE"
    fi
fi
echo "$$" > "$PID_FILE"

cleanup() {
    rm -f "$PID_FILE"
}
trap cleanup EXIT

echo "[$log_date] === 每日数据采集开始 ===" >> "$LOG_FILE" 2>&1
echo "[$log_date] 自我超时上限: ${SELF_MAX_RUNTIME}s" >> "$LOG_FILE" 2>&1

# ---- 第一步：检查本地数据库（缺失时从 NAS 恢复）----
if [ ! -f "$LOCAL_DB" ]; then
    echo "[$log_date] WARNING: 本地数据库不存在" >> "$LOG_FILE"
    if [ -d "$NAS_DIR" ] && [ -f "$NAS_DIR/stock_data_v2.duckdb" ]; then
        echo "[$log_date] 从 NAS 恢复整个数据目录..." >> "$LOG_FILE"
        mkdir -p "$LOCAL_DIR"
        rsync -a "$NAS_DIR/" "$LOCAL_DIR/"
        echo "[$log_date] NAS → 本地恢复完成" >> "$LOG_FILE"
    else
        echo "[$log_date] ERROR: 本地和 NAS 均无可用数据库，停止采集" >> "$LOG_FILE"
        exit 1
    fi
fi

# ============================================
# ---- 目标日期数据检查：TARGET_DATE 是否已采集 ----
# 新时间表凌晨 2:00 运行，TARGET_DATE 恒为上一交易日（get_target_date.py 保证）。
# 若 TARGET_DATE 行情已入库（如周日跑时周五已被周六采过），走周末新闻分支；
# 否则走主管线采集 TARGET_DATE（周六凌晨采周五数据也走这里）。
# ============================================
cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"

TARGET_DATA=$(timeout 60 "$VENV_DIR/bin/python" "$ROOT_DIR/scripts/check_target_data.py" "$LOCAL_DB" "$TARGET_DATE" 2>/dev/null || echo "no")
echo "[$log_date] TARGET_DATE=$TARGET_DATE 数据已采集: $TARGET_DATA" >> "$LOG_FILE" 2>&1

if [ "$TARGET_DATA" != "yes" ]; then
    echo "[$log_date] 目标日期行情未采集，执行主管线" >> "$LOG_FILE"
else
    echo "[$log_date] 目标日期行情已采集，执行新闻分支（RSS 采集 + 新闻分析）" >> "$LOG_FILE"
    
    # 新闻分支：RSS 采集
    bash "$ROOT_DIR/scripts/weekend_rss_news.sh" >> "$LOG_FILE" 2>&1 || true
    
    # 新闻分析 + LLM + 生成新闻报告文件（不发邮件，07:00 统一发送）
    # 注意：周末分支用"今天"日期（新闻是当天采集的），不传 TARGET_DATE
    bash "$ROOT_DIR/scripts/daily_news_analysis.sh" --no-trade --no-email >> "$LOG_FILE" 2>&1 || true
    
    echo "[$log_date] 新闻分支完成，退出" >> "$LOG_FILE"
    exit 0
fi

echo "[$log_date] 开始执行 run_daily (target=$TARGET_DATE)..." >> "$LOG_FILE"
set +e
timeout --kill-after=60s "$SELF_MAX_RUNTIME" "$VENV_DIR/bin/python" scripts/run_daily.py \
    --db-path "$LOCAL_DB" \
    --date "$TARGET_DATE" \
    >> "$LOG_FILE" 2>&1

RET=$?
set -e
echo "[$log_date] run_daily 退出码: $RET" >> "$LOG_FILE"
if [ "$RET" -eq 124 ] || [ "$RET" -eq 137 ]; then
    echo "[$log_date] ERROR: run_daily 超过 ${SELF_MAX_RUNTIME}s，已终止子进程" >> "$LOG_FILE"
    exit 1
fi
if [ "$RET" -ne 0 ]; then
    echo "[$log_date] ERROR: run_daily 执行失败，跳过分析报告" >> "$LOG_FILE"
    exit "$RET"
fi

# ---- 第三步：分析 + 推荐 + 同步 + 邮件报告 + 新闻（调用独立脚本）----
echo "[$log_date] 开始执行 daily_analysis_report.sh..." >> "$LOG_FILE"
set +e
bash "$ROOT_DIR/scripts/daily_analysis_report.sh" >> "$LOG_FILE" 2>&1
RET_REPORT=$?
set -e
echo "[$log_date] daily_analysis_report.sh 退出码: $RET_REPORT" >> "$LOG_FILE"

echo "[$log_date] === 采集完成 ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
