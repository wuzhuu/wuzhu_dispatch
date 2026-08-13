#!/usr/bin/env bash
# 每日推荐系统管线 — 在 daily_run.sh 采集完成后执行
# 生成观察池 → 更新跟踪 → 结算结果 → 评估 → 稳定性 → 图表 → 校验
# 定时: 工作日 01:00（确保 daily_run.sh 已完成）
#
# 依赖: daily_run.sh 完成后才有最新的 daily_price / score / factor 数据

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
if [ ! -d "$VENV_DIR" ] && [ -d "$ROOT_DIR/.venv" ]; then
    VENV_DIR="$ROOT_DIR/.venv"
fi
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_recommendation.log"
LOCAL_DB="$ROOT_DIR/stock_local_ai_data/stock_data_v2.duckdb"
RECOMMENDATION_CONFIG="$HOME/key/recommendation.yaml"
LLM_CONFIG="$HOME/key/stackAnalys_llm.yaml"

export TZ=Asia/Shanghai
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

log "=== 每日推荐系统管线开始 ==="

cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"

# ---- 第1步：生成当日的推荐池 ----
log "第1步: 生成推荐池..."
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/generate_stock_recommendations.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" >> "$LOG_FILE" 2>&1
RET1=$?
set -e
if [ $RET1 -eq 0 ]; then
    log "推荐池生成 ✅"
elif [ $RET1 -eq 124 ] || [ $RET1 -eq 137 ]; then
    log "推荐池生成超时 ⏰"
else
    log "推荐池生成异常 ❌ (exit=$RET1)"
fi

# ---- 第2步：更新跟踪数据（所有活跃批次） ----
log "第2步: 更新跟踪数据..."
set +e
timeout --kill-after=30s 600 "$VENV_DIR/bin/python" scripts/update_recommendation_tracking.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" >> "$LOG_FILE" 2>&1
RET2=$?
set -e
log "跟踪更新退出码: $RET2"

# ---- 第3步：结算已完成批次 ----
log "第3步: 结算已完成批次..."
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/finalize_recommendation_results.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" >> "$LOG_FILE" 2>&1
RET3=$?
set -e
log "结果结算退出码: $RET3"

# ---- 第4步：系统评估 ----
log "第4步: 系统评估..."
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/evaluate_recommendation_system.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" >> "$LOG_FILE" 2>&1
RET4=$?
set -e
log "系统评估退出码: $RET4"

# ---- 第5步：稳定性评估 ----
log "第5步: 稳定性评估..."
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/evaluate_recommendation_stability.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" >> "$LOG_FILE" 2>&1
RET5=$?
set -e
log "稳定性评估退出码: $RET5"

# ---- 第6步：生成图表 ----
log "第6步: 生成图表..."
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/generate_recommendation_charts.py \
    --db-path "$LOCAL_DB" >> "$LOG_FILE" 2>&1
RET6=$?
set -e
log "图表生成退出码: $RET6"

# ---- 第7步：系统校验 ----
log "第7步: 系统校验..."
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/validate_recommendation_system.py \
    --db-path "$LOCAL_DB" >> "$LOG_FILE" 2>&1
RET7=$?
set -e
log "系统校验退出码: $RET7"

log "=== 每日推荐系统管线结束 ==="
echo "" >> "$LOG_FILE"
