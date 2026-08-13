#!/usr/bin/env bash
# 每日 18:00 — 检查 GitHub 版本更新并合并
# 注意：合并后会恢复 SD 卡数据库路径配置

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
if [ ! -d "$VENV_DIR" ] && [ -d "$PROJECT_DIR/.venv" ]; then
    VENV_DIR="$PROJECT_DIR/.venv"
fi
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/git_update.log"

export TZ=Asia/Shanghai
log_date=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$LOG_DIR"
echo "[$log_date] === GitHub 版本检查 ===" >> "$LOG_FILE" 2>&1

cd "$PROJECT_DIR" || { echo "ERROR: 无法进入 $PROJECT_DIR" >> "$LOG_FILE"; exit 1; }

# 确保 git 安全目录
git config --global --add safe.directory "$ROOT_DIR" 2>/dev/null || true

git fetch origin >> "$LOG_FILE" 2>&1

LOCAL=$(git rev-parse HEAD 2>/dev/null)
REMOTE=$(git rev-parse origin/main 2>/dev/null)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[$log_date] 已是最新版本，无需更新。" >> "$LOG_FILE"
    exit 0
fi

echo "[$log_date] 检测到更新: $LOCAL -> $REMOTE" >> "$LOG_FILE"

# 拉取更新
git pull origin main >> "$LOG_FILE" 2>&1

# 恢复 SD 卡数据库路径（GitHub 上的 settings.yaml 使用相对路径）
source "$VENV_DIR/bin/activate"
STOCK_ROOT="$ROOT_DIR" python -c "
import os
import yaml
root = os.environ['STOCK_ROOT']
with open('config/settings.yaml') as f:
    cfg = yaml.safe_load(f)
cfg['database']['duckdb_path'] = '/mnt/sdcard/stock_local_ai_data/stock_data_v2.duckdb'
cfg['data']['root'] = '/mnt/sdcard/stock_local_ai_data'
cfg['data']['log_root'] = os.path.join(root, 'logs')
cfg['data']['report_root'] = os.path.join(root, 'stock_local_ai_data', 'reports')
with open('config/settings.yaml', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
print('settings.yaml 已恢复为 SD 卡路径')
" >> "$LOG_FILE" 2>&1

# 检查依赖是否有更新
if git diff HEAD@{1} HEAD --name-only | grep -q 'requirements.txt'; then
    echo "requirements.txt 有变化，重装依赖..." >> "$LOG_FILE"
    pip install -r requirements.txt -q >> "$LOG_FILE" 2>&1
fi

# 唤醒 Hermes 读取新文档，更新执行方案（非关键步骤，失败不影响后续）
echo "[$log_date] 唤醒 Hermes 审核更新..." >> "$LOG_FILE"
hermes -z "stackAnalys 项目刚刚从 GitHub 更新到 commit $(git rev-parse HEAD)。请读取仓库中的 README.md 和任何文档文件，理解当前推荐的执行方案，检查是否有影响运行配置的变更。如果发现需要调整，请执行必要的更改。" \
    --skills stock-data-pipeline-maintenance \
    --accept-hooks \
    >> "$LOG_FILE" 2>&1 || true

echo "[$log_date] 更新完成，当前版本: $(git rev-parse HEAD)" >> "$LOG_FILE"
