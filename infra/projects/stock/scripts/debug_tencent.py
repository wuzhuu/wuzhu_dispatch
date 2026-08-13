"""Debug fetch_tencent_hist inside the actual module."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stackAnalys"))

import time, json, requests
import pandas as pd
from src.collectors.free_market_sources import _sleep_before_tencent, get_tqdm, _to_tencent_symbol, _to_yyyymmdd

ts_code = "bj.920000"
symbol = _to_tencent_symbol(ts_code)
start = _to_yyyymmdd("20260101")
end = _to_yyyymmdd("20260601")

print(f"symbol={symbol}, start={start}, end={end}")

data_key = "day"
url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
year_chunks = [(2026, 2027)]

tqdm = get_tqdm()
big_df = pd.DataFrame()

for cs, ce in tqdm(year_chunks, leave=False):
    _sleep_before_tencent()
    params = {
        "_var": f"kline_day{cs}",
        "param": f"{symbol},day,{cs}-01-01,{ce}-01-01,640,",
        "r": str(time.time()),
    }
    print(f"\nChunk: param={params['param']}")
    r = requests.get(url, params=params, timeout=30)
    raw = r.text
    js = json.loads(raw[raw.find("={") + 1:])
    stock_data = js.get("data", {}).get(symbol, {})
    print(f"stock_data type={type(stock_data)}, keys={list(stock_data.keys()) if isinstance(stock_data, dict) else 'N/A'}")
    rows = stock_data.get(data_key) or stock_data.get("day") or stock_data.get("qfqday") or []
    print(f"rows={len(rows)}")
    
    if rows:
        temp_df = pd.DataFrame(rows)
        print(f"temp_df shape={temp_df.shape}")
        temp_df = temp_df.iloc[:, [0, 1, 2, 3, 4, 5, 7, 8]].copy()
        temp_df.columns = ["date", "open", "close", "high", "low", "volume", "pct_chg", "amount"]
        temp_df["volume"] = pd.to_numeric(temp_df["volume"], errors="coerce").fillna(0).astype("float64")
        temp_df["amount"] = pd.to_numeric(temp_df["amount"], errors="coerce").fillna(0).astype("float64")
        temp_df["pct_chg"] = pd.to_numeric(temp_df["pct_chg"], errors="coerce").fillna(0.0)
        
        big_df = pd.concat([big_df, temp_df], ignore_index=True)
        print(f"big_df now: {big_df.shape}")

if not big_df.empty:
    big_df = big_df.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    big_df = big_df.sort_values("date").reset_index(drop=True)
    
    start_iso = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_iso = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    print(f"\nFiltering: date >= {start_iso} and <= {end_iso}")
    print(f"Date range in data: {big_df['date'].min()} ~ {big_df['date'].max()}")
    
    big_df = big_df[(big_df["date"] >= start_iso) & (big_df["date"] <= end_iso)].copy()
else:
    print("big_df is EMPTY")

print(f"\nFinal shape: {big_df.shape}")
if not big_df.empty:
    print(f"First: {dict(big_df.iloc[0])}")
