#!/usr/bin/env bash
# 检测 git 是否有新更新，有则输出 diff 供 Hermes agent 分析
# 输出 = 有更新时的 diff 内容（注入 agent prompt）
# 静默 = 无更新
set -euo pipefail

ROOT_DIR="$HOME/stock"
LAST_CHECK_FILE="$ROOT_DIR/logs/.last_checked_git_commit"

cd "$ROOT_DIR"

# 获取远程最新状态
git fetch origin 2>/dev/null || true

# 检查 origin/main 是否与本地 HEAD 不同（有新的远端提交待拉取）
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "unknown")
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

if [ -f "$LAST_CHECK_FILE" ]; then
    LAST=$(cat "$LAST_CHECK_FILE")
    # 如果上次检查过的 remote hash 与当前相同且 local 没变 → 无新更新
    if [ "$REMOTE" = "$LAST" ] && [ "$REMOTE" = "$LOCAL" ]; then
        exit 0  # 无更新，静默退出
    fi
else
    LAST=""
fi

# 记录此次检测到的远端 hash（避免重复报告同一批远端更新）
echo "$REMOTE" > "$LAST_CHECK_FILE"

# 如果远端与本地 HEAD 相同 → 已是最新，无需输出
if [ "$REMOTE" = "$LOCAL" ]; then
    exit 0
fi

# 有远端更新待拉取！输出详细内容供 agent 分析
echo "=== Git 更新检测到 ==="
echo "检查时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "上次检测: ${LAST:-无记录}"
echo "本地 HEAD: $LOCAL"
echo "远端 origin/main: $REMOTE"
echo ""

echo "=== 更新日志（local..origin/main）==="
git log --oneline "$LOCAL..$REMOTE" 2>/dev/null || echo "(无法获取日志)"
echo ""
echo "=== 文件变动清单 ==="
git diff --stat "$LOCAL..$REMOTE" 2>/dev/null || echo "(无法获取统计)"
echo ""
echo "=== src/ + config/ 详细变更 ==="
git diff "$LOCAL..$REMOTE" -- stackAnalys/src/ config/ 2>/dev/null || true
echo ""
echo "=== README / 文档变更 ==="
git diff "$LOCAL..$REMOTE" -- stackAnalys/README.md stackAnalys/docs/ 2>/dev/null || true
