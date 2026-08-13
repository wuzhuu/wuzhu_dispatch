#!/usr/bin/env bash
# 早上 07:00 统一发送每日汇报（数据日报 + 新闻日报）
# 报告文件由凌晨 2:00 的 daily_run.sh / daily_analysis_report.sh 生成：
#   logs/daily_report_*.txt   数据日报（文件名含目标交易日）
#   logs/news_report_*.md     新闻日报
# 查找规则：最近 24 小时内生成的最新文件（兼容周末/工作日不同日期命名）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
if [ ! -d "$VENV_DIR" ] && [ -d "$PROJECT_DIR/.venv" ]; then
    VENV_DIR="$PROJECT_DIR/.venv"
fi
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/send_morning_reports.log"

export TZ=Asia/Shanghai
log_date=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$LOG_DIR"

echo "[$log_date] === 早上汇报发送开始 ===" >> "$LOG_FILE" 2>&1

# ---- 找最近 24h 的最新数据日报 / 新闻日报 ----
DATA_REPORT=$(find "$LOG_DIR" -maxdepth 1 -name "daily_report_*.txt" -mtime -1 -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
NEWS_REPORT=$(find "$LOG_DIR" -maxdepth 1 -name "news_report_*.md" -mtime -1 -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

# ---- 1. 发送数据日报 ----
if [ -n "$DATA_REPORT" ] && [ -s "$DATA_REPORT" ]; then
    RC_LEN=$(wc -c < "$DATA_REPORT")
    if [ "$RC_LEN" -ge 100 ]; then
        # 从文件名提取目标日期（daily_report_YYYY-MM-DD.txt）
        DATA_DATE=$(basename "$DATA_REPORT" | sed 's/daily_report_//;s/\.txt//')
        echo "[$log_date] 发送数据日报 ($DATA_REPORT, ${RC_LEN} 字符, target=$DATA_DATE)..." >> "$LOG_FILE" 2>&1
        "$VENV_DIR/bin/python" "$ROOT_DIR/scripts/send_report_email.py" "$DATA_REPORT" "$DATA_DATE" >> "$LOG_FILE" 2>&1
        echo "[$log_date] 数据日报发送完成" >> "$LOG_FILE" 2>&1
    else
        echo "[$log_date] WARNING: 数据日报内容过短（${RC_LEN}），跳过发送" >> "$LOG_FILE" 2>&1
    fi
else
    echo "[$log_date] WARNING: 未找到最近24h的数据日报文件" >> "$LOG_FILE" 2>&1
fi

# ---- 2. 发送新闻日报 ----
if [ -n "$NEWS_REPORT" ] && [ -s "$NEWS_REPORT" ]; then
    NEWS_LEN=$(wc -c < "$NEWS_REPORT")
    if [ "$NEWS_LEN" -ge 50 ]; then
        echo "[$log_date] 发送新闻日报 ($NEWS_REPORT, ${NEWS_LEN} 字符)..." >> "$LOG_FILE" 2>&1
        NEWS_FILE=$(mktemp /tmp/news_report_XXXXXX.md)
        EMAIL_SCRIPT=$(mktemp /tmp/email_send_XXXXXX.py)
        # 规范措辞：凌晨生成的日报是"昨日"数据，统一把"今日/今天"替换为"昨日"
        sed 's/今日/昨日/g; s/今天/昨日/g' "$NEWS_REPORT" > "$NEWS_FILE"
        cat > "$EMAIL_SCRIPT" << 'PYEOF'
import smtplib, sys, yaml, os
from email.mime.text import MIMEText
from email.header import Header

report_path = sys.argv[1]
with open(os.path.expanduser("~/key/email_smtp.yaml")) as f:
    cfg = yaml.safe_load(f)["smtp"]
sender = cfg.get("from_addr", cfg["username"])
recipient = cfg.get("to_addr", "1521045234@qq.com")
with open(report_path) as f:
    report_text = f.read().strip()
body = f"每日新闻分析日报（昨日）\n\n{report_text}\n\n---\n新闻管线自动发送，请勿回复。"
msg = MIMEText(body, "plain", "utf-8")
msg["From"] = sender
msg["To"] = recipient
msg["Subject"] = Header("【每日新闻摘要·昨日】", "utf-8")
try:
    server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
    server.starttls()
    server.login(cfg["username"], cfg["password"])
    server.sendmail(sender, [recipient], msg.as_string())
    server.quit()
    print("新闻邮件发送成功")
except Exception as e:
    print(f"新闻邮件发送失败: {e}", file=sys.stderr)
PYEOF
        "$VENV_DIR/bin/python" "$EMAIL_SCRIPT" "$NEWS_FILE" >> "$LOG_FILE" 2>&1
        rm -f "$NEWS_FILE" "$EMAIL_SCRIPT"
        echo "[$log_date] 新闻日报发送完成" >> "$LOG_FILE" 2>&1
    else
        echo "[$log_date] WARNING: 新闻日报内容过短（${NEWS_LEN}），跳过发送" >> "$LOG_FILE" 2>&1
    fi
else
    echo "[$log_date] WARNING: 未找到最近24h的新闻日报文件" >> "$LOG_FILE" 2>&1
fi

echo "[$log_date] === 早上汇报发送结束 ===" >> "$LOG_FILE" 2>&1
