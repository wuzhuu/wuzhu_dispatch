#!/usr/bin/env python3
"""检查目标交易日行情数据是否已采集。

用法: check_target_data.py <db_path> <target_date YYYY-MM-DD>
输出: yes / no
判定: v_daily_price 中 trade_date = target_date 且 ts_code LIKE 'sh.%' 的股票数 > 1000 视为已采集。
"""
import sys
import duckdb

db_path, target_date = sys.argv[1], sys.argv[2]
try:
    with duckdb.connect(db_path, read_only=True) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM v_daily_price "
            "WHERE trade_date = ?::DATE AND ts_code LIKE 'sh.%'",
            [target_date],
        ).fetchone()
        count = int(row[0]) if row else 0
    print("yes" if count >= 1000 else "no")
except Exception as e:
    print(f"no  # error: {e}", file=sys.stderr)
    sys.exit(1)
