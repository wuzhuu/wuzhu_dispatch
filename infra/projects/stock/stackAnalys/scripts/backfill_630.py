#!/usr/bin/env python3
"""补采 6/30 + 6/19 全部缺失数据（westock 批量 + baostock 兜底）"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import subprocess
import pandas as pd
from datetime import datetime

from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path


def parse_westock(stdout: str):
    """解析 westock CLI markdown 输出"""
    lines = stdout.strip().split("\n")
    table = [l for l in lines if l.startswith("|")]
    if len(table) < 3:
        return []
    hdr = [p.strip().lower() for p in table[0].split("|")[1:-1]]
    has_sym = "symbol" in hdr
    rows = []
    for line in table[2:]:
        parts = [p.strip() for p in line.split("|")[1:-1]]
        if has_sym and len(parts) >= 8:
            rows.append({"symbol": parts[0], "date": parts[1],
                "open": float(parts[2]), "close": float(parts[3]),
                "high": float(parts[4]), "low": float(parts[5]),
                "volume": float(parts[6]), "amount": float(parts[7])})
        elif not has_sym and len(parts) >= 7:
            rows.append({"symbol": None, "date": parts[0],
                "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]),
                "volume": float(parts[5]), "amount": float(parts[6])})
    return rows


def fill_date(target_date: str, store, description: str):
    have = set(r[0] for r in store.execute(
        "SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = ?", [target_date]
    ).fetchall())
    all_stocks = [r[0] for r in store.execute(
        "SELECT DISTINCT ts_code FROM stock_basic"
    ).fetchall()]
    missing = [s for s in all_stocks if s not in have]
    total = len(missing)
    print(f"\n{'='*60}", flush=True)
    print(f"补采 {target_date} ({description}): 缺失 {total}/{len(all_stocks)} 只", flush=True)
    if not total:
        print("✅ 无缺失", flush=True)
        return

    # Step 1: westock 批量（每批 60 只）
    codes = [s.replace(".", "") for s in missing]
    BATCH = 60
    batches = [codes[i:i+BATCH] for i in range(0, len(codes), BATCH)]
    print(f"Step1: westock 批量 → {len(batches)} 批", flush=True)

    written = 0
    for bi, batch in enumerate(batches):
        cmd = ["npx", "-y", "westock-data-skillhub@1.0.3",
               "kline", ",".join(batch), "--period", "day", "--limit", "5"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                continue
            recs = parse_westock(r.stdout)
        except:
            continue

        # 过滤目标日期行，拼 ts_code
        target_rows = [rec for rec in recs if rec["date"] == target_date]
        if not target_rows:
            continue
        records = []
        for rec in target_rows:
            sym = rec["symbol"]
            if not sym:
                sym = batch[0]
            ts_code = sym[:2] + "." + sym[2:] if "." not in sym else sym
            records.append({
                "ts_code": ts_code,
                "trade_date": rec["date"],
                "open": rec["open"],
                "high": rec["high"],
                "low": rec["low"],
                "close": rec["close"],
                "volume": int(rec["volume"]),
                "amount": rec["amount"],
                "source": "westock-data.kline",
            })
        if records:
            df = pd.DataFrame(records)
            store.upsert_daily_price(df)
            written += len(records)

        if (bi + 1) % 10 == 0:
            print(f"  westock: {bi+1}/{len(batches)} 批, 已写{written}行", flush=True)

    print(f"Step1 完成: westock 写入 {written} 行", flush=True)

    # Step 2: baostock/eastmoney 补漏（逐只）
    new_have = set(r[0] for r in store.execute(
        "SELECT DISTINCT ts_code FROM v_daily_price WHERE trade_date = ?", [target_date]
    ).fetchall())
    still_missing = [s for s in missing if s not in new_have]
    if still_missing:
        print(f"Step2: baostock 补漏 {len(still_missing)} 只...", flush=True)
        from src.collectors.market_provider import MarketDataProvider
        p = MarketDataProvider()
        batch_rows = []
        for i, ts_code in enumerate(still_missing[:500]):  # 最多补 500 只
            try:
                df = p.get_daily_price_multi_source(ts_code, target_date, target_date,
                    ["baostock", "eastmoney", "westock"])
                if df is not None and not df.empty:
                    batch_rows.append(df)
            except:
                pass
            if len(batch_rows) >= 200:
                store.upsert_daily_price(pd.concat(batch_rows, ignore_index=True))
                batch_rows = []
            if (i+1) % 100 == 0:
                print(f"  baostock: {i+1}/{len(still_missing)}", flush=True)
        if batch_rows:
            store.upsert_daily_price(pd.concat(batch_rows, ignore_index=True))
        p.close_baostock_session()

    final = store.execute(
        "SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?", [target_date]
    ).fetchone()[0]
    print(f"✅ {target_date} 最终行数: {final}", flush=True)


def main():
    settings = load_settings()
    store = LakeStore(db_path=resolve_db_path(settings))
    fill_date("2026-06-30", store, "昨日缺失")
    fill_date("2026-06-19", store, "6/19 完全缺失")
    store.close()


if __name__ == "__main__":
    main()
