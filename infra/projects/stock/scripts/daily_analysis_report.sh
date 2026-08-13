#!/usr/bin/env bash
# 每日分析报告脚本 — 在 run_daily 采集完成后执行
# 包含: 分析(daily_maintenance_job) → 推荐评测 → 同步 → 生成报告文件 → 新闻分析 → 新闻报告文件
# 邮件发送由 send_morning_reports.sh 在 07:00 统一执行
# 
# 用法: bash daily_analysis_report.sh [--db-path PATH]
# 环境变量: LOCAL_DB, VENV_DIR, PROJECT_DIR, ROOT_DIR, RECOMMENDATION_CONFIG, TARGET_DATE

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/stackAnalys"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
if [ ! -d "$VENV_DIR" ] && [ -d "$PROJECT_DIR/.venv" ]; then
    VENV_DIR="$PROJECT_DIR/.venv"
fi
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_run.log"
LOCAL_DIR="$ROOT_DIR/stock_local_ai_data"
LOCAL_DB="${LOCAL_DB:-$LOCAL_DIR/stock_data_v2.duckdb}"
RECOMMENDATION_CONFIG="${RECOMMENDATION_CONFIG:-$HOME/key/recommendation.yaml}"

export TZ=Asia/Shanghai
log_date=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$LOG_DIR"

echo "[$log_date] === 每日分析报告开始 ===" >> "$LOG_FILE"

# ---- 第一步：执行分析（daily_maintenance_job）----
echo "[$log_date] 开始执行 daily_maintenance_job（分析+图表）..." >> "$LOG_FILE"
cd "$PROJECT_DIR"
set +e
timeout --kill-after=60s 3600 "$VENV_DIR/bin/python" scripts/daily_maintenance_job.py \
    --db-path "$LOCAL_DB" --with-visuals \
    >> "$LOG_FILE" 2>&1
RET_MAINT=$?
set -e
echo "[$log_date] daily_maintenance_job 退出码: $RET_MAINT" >> "$LOG_FILE"

# ---- 第二步：推荐系统评测 ----
echo "[$log_date] 开始执行推荐评测（生成、跟踪、结算、稳定性、图表）..." >> "$LOG_FILE"
cd "$PROJECT_DIR"

# 2a. 生成今日新批次
set +e
timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/generate_stock_recommendations.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" \
    --date "$(date +%Y-%m-%d)" \
    >> "$LOG_FILE" 2>&1
RET_REC_GEN=$?
set -e
echo "[$log_date] 推荐生成 退出码: $RET_REC_GEN" >> "$LOG_FILE"

# 2b. 更新推荐跟踪
set +e
timeout --kill-after=30s 180 "$VENV_DIR/bin/python" scripts/update_recommendation_tracking.py \
    --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" \
    --date "$(date +%Y-%m-%d)" \
    >> "$LOG_FILE" 2>&1
RET_REC_TRACK=$?
set -e
echo "[$log_date] 推荐跟踪 退出码: $RET_REC_TRACK" >> "$LOG_FILE"

if [ $RET_REC_TRACK -ne 0 ] && [ $RET_REC_TRACK -ne 124 ]; then
    echo "[$log_date] WARNING: 推荐跟踪失败，跳过后续推荐步骤" >> "$LOG_FILE"
else
    # 2c. 结算到期结果
    set +e
    timeout --kill-after=30s 180 "$VENV_DIR/bin/python" scripts/finalize_recommendation_results.py \
        --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" \
        --start "$(date -d '-10 days' +%Y-%m-%d)" --end "$(date +%Y-%m-%d)" \
        >> "$LOG_FILE" 2>&1
    RET_REC_FIN=$?
    set -e
    echo "[$log_date] 推荐结算 退出码: $RET_REC_FIN" >> "$LOG_FILE"

    # 2d. 稳定性评估
    set +e
    timeout --kill-after=30s 180 "$VENV_DIR/bin/python" scripts/evaluate_recommendation_stability.py \
        --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" \
        >> "$LOG_FILE" 2>&1
    RET_REC_STAB=$?
    set -e
    echo "[$log_date] 推荐稳定性 退出码: $RET_REC_STAB" >> "$LOG_FILE"

    # 2e. 系统评估
    set +e
    timeout --kill-after=30s 120 "$VENV_DIR/bin/python" scripts/evaluate_recommendation_system.py \
        --db-path "$LOCAL_DB" --config "$RECOMMENDATION_CONFIG" \
        >> "$LOG_FILE" 2>&1
    RET_REC_EVAL=$?
    set -e
    echo "[$log_date] 推荐系统评估 退出码: $RET_REC_EVAL" >> "$LOG_FILE"

    # 2f. 生成图表
    set +e
    timeout --kill-after=30s 120 "$VENV_DIR/bin/python" scripts/generate_recommendation_charts.py \
        --db-path "$LOCAL_DB" \
        >> "$LOG_FILE" 2>&1
    RET_REC_CHART=$?
    set -e
    echo "[$log_date] 推荐图表 退出码: $RET_REC_CHART" >> "$LOG_FILE"
fi

# ---- 第三步：同步到 NAS（限时30秒，超时则跳过不阻塞邮件）----
echo "[$log_date] 执行数据同步..." >> "$LOG_FILE"
timeout 30 bash "$ROOT_DIR/scripts/sync_data.sh" >> "$LOG_FILE" 2>&1 || echo "[$log_date] 数据同步跳过/失败（不影响邮件）" >> "$LOG_FILE"

# ---- 第四步：生成数据报告文件（不发送邮件，07:00 由 send_morning_reports.sh 统一发送）----
echo "[$log_date] 开始生成数据报告文件..." >> "$LOG_FILE"

# 报告目标日期：优先用 TARGET_DATE（凌晨运行=上一交易日），否则用今天
REPORT_TARGET="${TARGET_DATE:-$(date +%Y-%m-%d)}"
REPORT_FILE="$LOG_DIR/daily_report_${REPORT_TARGET}.txt"
set +e
"$VENV_DIR/bin/python" - "$LOCAL_DB" "$REPORT_TARGET" > "$REPORT_FILE" 2>> "$LOG_FILE" << 'PYEOF'
import sys, duckdb
db_path = sys.argv[1]; target_date = sys.argv[2]
db = duckdb.connect(db_path, read_only=True); parts = []
parts.append("━" * 50); parts.append("日数据报告（昨日） — %s" % target_date)
DATA_OK = True
try:
    sd = db.execute("SELECT MAX(trade_date) FROM v_score_daily").fetchone()
    if sd and sd[0]:
        sd_date = str(sd[0])[:10]
        sh_cnt = db.execute("SELECT COUNT(*) FROM v_score_daily WHERE trade_date=?::TIMESTAMP AND ts_code LIKE 'sh.%'", [sd_date]).fetchone()[0]
        sz_cnt = db.execute("SELECT COUNT(*) FROM v_score_daily WHERE trade_date=?::TIMESTAMP AND ts_code LIKE 'sz.%'", [sd_date]).fetchone()[0]
        if sh_cnt < 1000:
            parts.append("❌ 数据异常：沪市评分数据缺失（SH=%d只，预期~2300只），跳过排名和荐股" % sh_cnt)
            DATA_OK = False
        if sz_cnt < 1000:
            parts.append("❌ 数据异常：深市评分数据缺失（SZ=%d只，预期~2900只），跳过排名和荐股" % sz_cnt)
            DATA_OK = False
        if DATA_OK:
            parts.append("数据完整性: ✅ 沪市%d只 深市%d只 评分日期:%s" % (sh_cnt, sz_cnt, sd_date))
except: pass
parts.append("━━━ 市场分析 ━━━")
try:
    s = db.execute("SELECT above_ma20_ratio,above_ma60_ratio,stock_count FROM market_state_daily ORDER BY trade_date DESC LIMIT 1").fetchone()
    if s: parts.append("市场广度: 站上MA20 %.0f%% | 站上MA60 %.0f%% | 统计 %d只" % (s[0]*100 if s[0] else 0, s[1]*100 if s[1] else 0, s[2]))
except: pass
try:
    r = db.execute("SELECT report_markdown FROM v_daily_analysis_report ORDER BY trade_date DESC LIMIT 1").fetchone()
    if r and r[0]:
        for line in r[0].split("\n"):
            if "market_regime" in line: parts.append("市场状态: %s" % line.split(":")[-1].strip()); break
except: pass
parts.append("━━━ 荐股组合表现 ━━━")
try:
    lp = db.execute("SELECT active_batch_count,active_position_count,gross_daily_return,cumulative_return FROM v_recommendation_live_portfolio_daily ORDER BY trading_date DESC LIMIT 1").fetchone()
    if lp: parts.append("跟踪批次: %d | 持仓股票: %d只" % (lp[0], lp[1])); parts.append("当日收益: %s | 累计收益: %s" % (("%.2f%%" % (lp[2]*100)) if lp[2] else "N/A", ("%.2f%%" % (lp[3]*100)) if lp[3] else "N/A"))
except: pass
parts.append("━━━ 最新一批部署 ━━━")
try:
    b = db.execute("SELECT signal_date,universe_count,eligible_count,selected_count,batch_id FROM v_recommendation_batch WHERE selector_name='raw_top_n' ORDER BY trade_date DESC LIMIT 1").fetchone()
    if b:
        sd,uni,eligible,selected,bid = b
        parts.append("信号日期: %s | 选中: %d只" % (str(sd.date()), selected))
        parts.append("候选池: %d只 → 合格 %d只 → 选中 %d只" % (uni, eligible, selected))
        for it in db.execute("""SELECT ri.rank,ri.name,ri.ts_code,ri.final_score,ri.risk_level,re.factor_summary,re.industry_summary FROM v_recommendation_item ri LEFT JOIN v_recommendation_explanation re ON ri.batch_id=re.batch_id AND ri.ts_code=re.ts_code WHERE ri.batch_id=? ORDER BY ri.rank""", [bid]).fetchall():
            parts.append(""); parts.append("%d. %s (%s) — 评分 %.4f, 风险 %s" % (it[0], it[1], it[2], it[3] if it[3] else 0, it[4]))
            if it[5]: parts.append("    因子: %s" % it[5])
            if it[6]: parts.append("    行业: %s" % it[6])
        parts.append(""); parts.append("batch_id: %s... | ⏳ 还剩%d个交易日评估" % (str(bid)[:8], 30))
except Exception as e: parts.append("⚠️ %s" % str(e)[:50])
if not DATA_OK:
    parts.append(""); parts.append("⚠️ 评分数据不完整，跳过排名和荐股分析")
    db.close(); print("\n".join(parts)); sys.exit(0)
try:
    r = db.execute("SELECT report_markdown FROM v_daily_analysis_report ORDER BY trade_date DESC LIMIT 1").fetchone()
    if r and r[0]: parts.append(""); parts.append("━━━ Top20 综合排名 ━━━"); parts.append(r[0])
except: pass
parts.append("━━━ 历史荐股评估 ━━━")
try:
    ev = db.execute("SELECT completed_batch_count,completed_stock_count,average_return_30d,median_return_30d,positive_hit_rate,average_max_drawdown,profit_loss_ratio FROM v_recommendation_evaluation_daily ORDER BY evaluation_date DESC LIMIT 1").fetchone()
    if ev:
        parts.append("已完成: %d批次 / %d只股票" % (ev[0], ev[1]))
        if ev[2]: parts.append("30日平均收益: %.2f%%" % (ev[2]*100))
        if ev[3]: parts.append("30日中位收益: %.2f%%" % (ev[3]*100))
        try:
            ar = db.execute("SELECT trade_date, rolling_annual_net_return, annualized_return_to_date FROM v_recommendation_rolling_return_daily WHERE annual_return_status='OK' ORDER BY trade_date DESC LIMIT 1").fetchone()
            if ar and ar[1] is not None:
                parts.append("滚动年度收益: %.2f%%" % (ar[1]*100))
        except: pass
        if ev[4]: parts.append("正向命中率: %.1f%%" % (ev[4]*100))
        if ev[5]: parts.append("平均最大回撤: %.2f%%" % (ev[5]*100))
        if ev[6]: parts.append("盈亏比: %.2f" % ev[6])
except: pass
parts.append("━━━ 排名稳定性分析 ━━━")
try:
    st = db.execute("SELECT overlap_ratio_1d,turnover_1d,new_entry_count,dropout_count,mean_rank_change,mean_consecutive_days FROM v_recommendation_stability_daily WHERE is_first_observation=false ORDER BY trade_date DESC LIMIT 1").fetchone()
    if st:
        if st[0]: parts.append("当日重叠率: %.0f%%" % (st[0]*100))
        if st[1]: parts.append("当日换手率: %.0f%%" % (st[1]*100))
        parts.append("新进入: %d只 | 退出: %d只" % (st[2], st[3]))
        if st[4]: parts.append("平均排名变化: %.1f位" % st[4])
        if st[5]: parts.append("平均连续留存: %.1f天" % st[5])
except: pass
db.close(); print("\n".join(parts))
PYEOF
REPORT_RET=$?
set -e

if [ $REPORT_RET -eq 0 ] && [ -s "$REPORT_FILE" ]; then
    RC_LEN=$(wc -c < "$REPORT_FILE")
    echo "[$log_date] 数据报告文件已生成: $REPORT_FILE (${RC_LEN} 字符)" >> "$LOG_FILE"
    if [ "$RC_LEN" -lt 100 ]; then
        echo "[$log_date] WARNING: 报告内容过短（${RC_LEN}），保留文件待 07:00 发送时再判断" >> "$LOG_FILE"
    fi
else
    echo "[$log_date] ERROR: 报告生成失败（exit=$REPORT_RET），无报告文件" >> "$LOG_FILE"
fi

# ---- 第五步：新闻分析 + 生成新闻报告文件（邮件 07:00 统一发送）----
echo "[$log_date] 开始执行新闻分析..." >> "$LOG_FILE"
cd "$PROJECT_DIR"

# 5a. 关键词规则分析
set +e
"$VENV_DIR/bin/python" scripts/build_news_analysis.py --db-path "$LOCAL_DB" >> "$LOG_FILE" 2>&1
RET_NEWS_A=$?
set -e
echo "[$log_date] 新闻关键词分析 退出码: $RET_NEWS_A" >> "$LOG_FILE"

# 5b. LLM 新闻分析（限30条）
set +e
PYTHONUNBUFFERED=1 timeout --kill-after=30s 900 "$VENV_DIR/bin/python" scripts/analyze_news_with_llm.py \
    --db-path "$LOCAL_DB" --config "$HOME/key/stackAnalys_llm.yaml" --limit 30 >> "$LOG_FILE" 2>&1
RET_NEWS_B=$?
set -e
echo "[$log_date] LLM 新闻分析 退出码: $RET_NEWS_B" >> "$LOG_FILE"

# 5c. 日报摘要生成（用 TARGET_DATE，凌晨运行时为上一交易日）
TODAY="${TARGET_DATE:-$(date '+%Y-%m-%d')}"
set +e
PYTHONUNBUFFERED=1 timeout --kill-after=30s 300 "$VENV_DIR/bin/python" scripts/generate_daily_news_digest.py \
    --db-path "$LOCAL_DB" --config "$HOME/key/stackAnalys_llm.yaml" --date "$TODAY" --force >> "$LOG_FILE" 2>&1
RET_NEWS_C=$?
set -e
echo "[$log_date] 日报摘要生成 退出码: $RET_NEWS_C" >> "$LOG_FILE"

# 5d. 生成新闻报告文件（不发送邮件）
NEWS_REPORT_FILE="$LOG_DIR/news_report_${TODAY}.md"
NEWS_REPORT=$("$VENV_DIR/bin/python" - "$LOCAL_DB" "$TODAY" 2>> "$LOG_FILE" << 'PYEOF'
import sys, duckdb
db_path = sys.argv[1]
target_date = sys.argv[2]
try:
    with duckdb.connect(db_path, read_only=True) as conn:
        row = conn.execute("""
            SELECT report_markdown FROM v_daily_news_llm_digest
            WHERE trade_date = ? AND status = 'SUCCESS'
            ORDER BY created_at DESC LIMIT 1
        """, [target_date]).fetchone()
        if row and row[0]:
            print(row[0])
            sys.exit(0)
except: pass
try:
    with duckdb.connect(db_path, read_only=True) as conn:
        row = conn.execute("""
            SELECT market_summary, news_count, risk_news_count, industry_summary
            FROM v_daily_news_summary WHERE trade_date = ? ORDER BY trade_date DESC LIMIT 1
        """, [target_date]).fetchone()
        if row:
            print(f"# 每日新闻摘要（关键词规则版）\n\n**交易日（昨日）**: {target_date}\n\n**新闻数**: {row[1]}\n**风险事件**: {row[2]}\n\n## 市场概况\n{row[0]}\n\n## 行业摘要\n{row[3] if row[3] else '（无）'}")
            sys.exit(0)
except: pass
print("暂无新闻摘要数据")
PYEOF
)

NEWS_LEN="${#NEWS_REPORT}"
echo "[$log_date] 新闻报告内容长度: ${NEWS_LEN} 字符" >> "$LOG_FILE"

if [ -z "$NEWS_REPORT" ] || [ "$NEWS_LEN" -lt 50 ]; then
    echo "[$log_date] WARNING: 新闻报告内容过短（${NEWS_LEN}），不生成文件" >> "$LOG_FILE"
else
    echo "$NEWS_REPORT" > "$NEWS_REPORT_FILE"
    echo "[$log_date] 新闻报告文件已生成: $NEWS_REPORT_FILE" >> "$LOG_FILE"
fi

echo "[$log_date] 本地数据目录: $(du -sh "$LOCAL_DIR" 2>/dev/null | cut -f1)" >> "$LOG_FILE"
echo "[$log_date] === 每日分析报告结束 ====" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
