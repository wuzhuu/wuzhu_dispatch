from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path
from src.utils.proxy import disable_proxy_if_configured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update stock_industry_map from BaoStock industry classification.")
    add_db_path_arg(parser)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print summary without writing.")
    parser.add_argument("--force", action="store_true", help="Write a new current mapping version even when rows already exist.")
    return parser.parse_args()


def main() -> None:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    db_path = Path(resolve_db_path(settings, args.db_path)).expanduser().resolve(strict=False)
    store = LakeStore(db_path=db_path)
    try:
        store.connect()
        rows = fetch_baostock_industry_map()
        if rows.empty:
            warning = "WARNING: BaoStock query_stock_industry returned no rows; stock_industry_map was not changed."
            print(warning)
            _log(store, "WARN", warning)
            return
        rows = _attach_stock_names(store, rows)
        print(f"candidate rows: {len(rows)}")
        print(f"industry_count: {rows['industry_name'].nunique(dropna=True)}")
        print(f"lake_root: {store.lake_root}")
        if args.dry_run:
            print("dry_run: true")
            return
        written = store.upsert_stock_industry_map(rows)
        store.refresh_views()
        print(f"stock_industry_map rows written: {written}")
        print("view refreshed: v_stock_industry_map")
    except Exception as exc:
        warning = f"WARNING: update_stock_industry_map failed: {exc!r}"
        print(warning)
        try:
            _log(store, "WARN", warning)
        except Exception:
            pass
    finally:
        store.close()


def fetch_baostock_industry_map() -> pd.DataFrame:
    try:
        import baostock as bs
    except Exception as exc:
        print(f"WARNING: baostock import failed: {exc!r}")
        return _empty()

    login_result = None
    try:
        login_result = bs.login()
        if getattr(login_result, "error_code", "0") != "0":
            print(f"WARNING: baostock login failed: {getattr(login_result, 'error_msg', '')}")
            return _empty()
        rs = bs.query_stock_industry()
        if getattr(rs, "error_code", "0") != "0":
            print(f"WARNING: baostock query_stock_industry failed: {getattr(rs, 'error_msg', '')}")
            return _empty()
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        df = pd.DataFrame(data, columns=rs.fields)
    except Exception as exc:
        print(f"WARNING: baostock query_stock_industry raised: {exc!r}")
        return _empty()
    finally:
        if login_result is not None:
            try:
                bs.logout()
            except Exception:
                pass

    if df.empty:
        return _empty()
    return normalize_baostock_industry(df)


def normalize_baostock_industry(df: pd.DataFrame) -> pd.DataFrame:
    now = pd.Timestamp.now()
    source_cols = {col.lower(): col for col in df.columns}
    code_col = source_cols.get("code")
    name_col = source_cols.get("code_name") or source_cols.get("name")
    industry_col = source_cols.get("industry") or source_cols.get("industry_name")
    date_col = source_cols.get("update_date") or source_cols.get("out_date")
    out = pd.DataFrame()
    out["ts_code"] = df[code_col].map(_normalize_ts_code) if code_col else pd.NA
    out["name"] = df[name_col] if name_col else pd.NA
    out["industry_source"] = "baostock.query_stock_industry"
    out["industry_code_l1"] = pd.NA
    out["industry_name_l1"] = df[industry_col] if industry_col else pd.NA
    out["industry_code_l2"] = pd.NA
    out["industry_name_l2"] = pd.NA
    out["industry_code_l3"] = pd.NA
    out["industry_name_l3"] = pd.NA
    out["industry_name"] = df[industry_col] if industry_col else pd.NA
    out["sw_code_2021"] = pd.NA
    out["source"] = "baostock.query_stock_industry"
    out["effective_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date if date_col else now.date()
    out["is_current"] = True
    out["created_at"] = now
    out["updated_at"] = now
    out["fetched_at"] = now
    out = out.dropna(subset=["ts_code", "industry_name"])
    return out.drop_duplicates(["ts_code", "industry_source", "effective_date"], keep="first").reset_index(drop=True)


def _attach_stock_names(store: LakeStore, rows: pd.DataFrame) -> pd.DataFrame:
    try:
        names = store.execute("SELECT ts_code, name FROM v_stock_basic_latest WHERE name IS NOT NULL").df()
    except Exception:
        return rows
    if names.empty:
        return rows
    out = rows.merge(names.drop_duplicates("ts_code"), on="ts_code", how="left", suffixes=("", "_basic"))
    out["name"] = out["name"].fillna(out.get("name_basic"))
    return out.drop(columns=[col for col in ["name_basic"] if col in out.columns])


def _normalize_ts_code(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip().lower()
    if "." in text:
        prefix, code = text.split(".", 1)
        if prefix in {"sh", "sz", "bj"}:
            return f"{prefix}.{code.zfill(6)}"
        if code in {"sh", "sz", "bj"}:
            return f"{code}.{prefix.zfill(6)}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6:
        return None
    code = digits[-6:]
    if code.startswith(("6", "9")):
        return f"sh.{code}"
    if code.startswith(("8", "4")):
        return f"bj.{code}"
    return f"sz.{code}"


def _log(store: LakeStore, status: str, message: str) -> None:
    store.execute(
        "INSERT INTO daily_update_log (log_time, task_name, target, status, message) VALUES (?, ?, ?, ?, ?)",
        [pd.Timestamp.now(), "stock_industry_map", "baostock", status, message],
    )


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts_code", "name", "industry_name", "source", "fetched_at"])


if __name__ == "__main__":
    main()
