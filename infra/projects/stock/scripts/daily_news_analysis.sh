#!/usr/bin/env bash
# 每日新闻分析 + LLM 日报 + 邮件发送
# 用法: daily_news_analysis.sh [--no-trade] [--no-email]
#   --no-trade  非交易日模式，邮件标题/正文加 "闭盘" 标注
#   --no-email  只生成新闻报告文件（logs/news_report_<date>.md），不发送邮件
#               （07:00 由 send_morning_reports.sh 统一发送）

set -euo pipefail

NO_TRADE=false
NO_EMAIL=false
for arg in "$@"; do
    case "$arg" in
        --no-trade) NO_TRADE=true ;;
        --no-email) NO_EMAIL=true ;;
    esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_news_analysis.log"
LOCAL_DB="$ROOT_DIR/stock_local_ai_data/stock_data_v2.duckdb"
LLM_CONFIG="$HOME/key/stackAnalys_llm.yaml"
EMAIL_KEY="$HOME/key/email_smtp.yaml"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

log "=== 每日新闻分析开始 ==="

# ---- 第1步：关键词规则分析（快） ----
log "第1步: 关键词规则分析..."
set +e
"$VENV_DIR/bin/python" scripts/build_news_analysis.py --db-path "$LOCAL_DB" >> "$LOG_FILE" 2>&1
RET1=$?
set -e
if [ $RET1 -eq 0 ]; then
    log "关键词分析完成 ✅"
else
    log "关键词分析失败 ❌ (exit=$RET1)"
fi

# ---- 第2步：LLM 新闻分析（限30条，超时15分钟） ----
log "第2步: LLM 新闻分析..."
set +e
PYTHONUNBUFFERED=1 timeout --kill-after=30s 900 "$VENV_DIR/bin/python" scripts/analyze_news_with_llm.py \
    --db-path "$LOCAL_DB" --config "$LLM_CONFIG" --limit 30 >> "$LOG_FILE" 2>&1
RET2=$?
set -e
if [ $RET2 -eq 0 ]; then
    log "LLM 分析完成 ✅"
elif [ $RET2 -eq 124 ] || [ $RET2 -eq 137 ]; then
    log "LLM 分析超时 ⏰ (exit=$RET2)，继续走 fallback"
else
    log "LLM 分析异常 ❌ (exit=$RET2)，继续走 fallback"
fi

# ---- 第3步：LLM 日报摘要生成 ----
log "第3步: LLM 日报摘要生成..."
TODAY="${TARGET_DATE:-$(date '+%Y-%m-%d')}"
set +e
PYTHONUNBUFFERED=1 timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/generate_daily_news_digest.py \
    --db-path "$LOCAL_DB" --config "$LLM_CONFIG" --date "$TODAY" --force >> "$LOG_FILE" 2>&1
RET3=$?
set -e
if [ $RET3 -eq 0 ]; then
    log "日报摘要生成完成 ✅"
else
    log "日报摘要生成异常 ❌ (exit=$RET3)"
fi

# ---- 第4步：生成新闻报告文件；--no-email 时不发送，07:00 统一发 ----
if [ "$NO_EMAIL" = true ]; then
    log "第4步: 生成新闻报告文件（--no-email，不发送邮件）..."
else
    log "第4步: 生成并发送邮件日报..."
fi
set +e

REPORT_CONTENT=$("$VENV_DIR/bin/python" - "$LOCAL_DB" "$TODAY" 2>> "$LOG_FILE" << 'PYEOF'
import sys, duckdb, json

db_path = sys.argv[1]
target_date = sys.argv[2]

try:
    with duckdb.connect(db_path, read_only=True) as conn:
        # 优先用 LLM digest
        row = conn.execute("""
            SELECT report_markdown, market_summary, industry_summary, stock_summary,
                   risk_summary, uncertainty_summary, top_topics_json, source_news_count
            FROM v_daily_news_llm_digest
            WHERE trade_date = ? AND status = 'SUCCESS'
            ORDER BY created_at DESC LIMIT 1
        """, [target_date]).fetchone()
        if row and row[0]:
            print(row[0])  # report_markdown
            sys.exit(0)
except Exception:
    pass

# Fallback: 关键词规则版日报
try:
    with duckdb.connect(db_path, read_only=True) as conn:
        row = conn.execute("""
            SELECT market_summary, news_count, top_tags_json, risk_news_count, industry_summary
            FROM v_daily_news_summary
            WHERE trade_date = ? ORDER BY trade_date DESC LIMIT 1
        """, [target_date]).fetchone()
        if row:
            print(f"# 每日新闻摘要（关键词规则版）\n")
            print(f"**交易日（昨日）**: {target_date}\n")
            print(f"**新闻数**: {row[1]}")
            print(f"**风险事件**: {row[3]}")
            print(f"\n## 市场概况\n{row[0]}")
            print(f"\n## 行业摘要\n{row[4] if row[4] else '（无）'}")
            sys.exit(0)
except Exception:
    pass

print("WARNING: 暂无新闻摘要数据")
PYEOF
)

RC_LEN="${#REPORT_CONTENT}"
log "报告内容长度: ${RC_LEN} 字符"

if [ -z "$REPORT_CONTENT" ] || [ "$RC_LEN" -lt 50 ]; then
    log "WARNING: 报告内容过短（${RC_LEN}），跳过邮件发送"
else
    if [ "$NO_EMAIL" = true ]; then
        # 只生成报告文件，供 07:00 send_morning_reports.sh 统一发送
        NEWS_REPORT_FILE="$LOG_DIR/news_report_${TODAY}.md"
        echo "$REPORT_CONTENT" > "$NEWS_REPORT_FILE"
        log "新闻报告文件已生成: $NEWS_REPORT_FILE"
    else
        log "发送邮件..."
        # ⚠️ 不能用 pipe+heredoc 组合：sys.stdin.read()会读到空（HEREDOC覆盖stdin）
        NEWS_FILE=$(mktemp /tmp/news_report_XXXXXX.md)
        EMAIL_SCRIPT=$(mktemp /tmp/email_send_XXXXXX.py)

        # 非交易日模式：邮件正文前置闭盘标注
        if [ "$NO_TRADE" = true ]; then
            REPORT_CONTENT="📢 昨日闭盘，只有新闻摘要，没有数据分析\n\n$REPORT_CONTENT"
        fi

        echo "$REPORT_CONTENT" > "$NEWS_FILE"
        cat > "$EMAIL_SCRIPT" << 'PYEOF'
import smtplib, sys, yaml, os
from email.mime.text import MIMEText
from email.header import Header

report_path = sys.argv[1]
with open(os.path.expanduser("~/key/email_smtp.yaml")) as f:
    cfg = yaml.safe_load(f)["smtp"]
with open(report_path) as f:
    report_text = f.read().strip()
body = f"每日新闻分析日报（昨日）\n\n{report_text}\n\n---\n新闻管线自动发送，请勿回复。"
msg = MIMEText(body, "plain", "utf-8")
msg["From"] = cfg["username"]
msg["To"] = cfg["username"]
msg["Subject"] = Header("【每日新闻摘要·昨日】", "utf-8")
try:
    server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
    server.starttls()
    server.login(cfg["username"], cfg["password"])
    server.sendmail(cfg["username"], [cfg["username"]], msg.as_string())
    server.quit()
    print("邮件发送成功")
except Exception as e:
    print(f"邮件发送失败: {e}", file=sys.stderr)
PYEOF
        "$VENV_DIR/bin/python" "$EMAIL_SCRIPT" "$NEWS_FILE" 2>> "$LOG_FILE"
        rm -f "$NEWS_FILE" "$EMAIL_SCRIPT"
        log "邮件发送步骤完成"
    fi
fi

log "=== 每日新闻分析结束 ==="
