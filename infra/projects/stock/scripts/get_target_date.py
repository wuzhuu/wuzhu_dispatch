#!/usr/bin/env python3
"""计算管线的目标交易日。

凌晨 2:00 运行时 `date` 是"今天"（未开盘），行情目标日期应为上一交易日。
规则：从昨天开始向前找最近一个交易日（is_trade_day），输出 YYYY-MM-DD。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path("/home/stock/stock/stackAnalys")))
from src.utils.calendar import is_trade_day  # noqa: E402

d = date.today() - timedelta(days=1)
for _ in range(30):  # 最多回溯 30 天（覆盖长假）
    if is_trade_day(d.strftime("%Y%m%d")):
        print(d.strftime("%Y-%m-%d"))
        sys.exit(0)
    d -= timedelta(days=1)
print(date.today().strftime("%Y-%m-%d"), file=sys.stderr)
sys.exit(1)
