from __future__ import annotations

import os
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.proxy import disable_proxy_if_configured
SAMPLE_DIR = PROJECT_ROOT / "data" / "samples"
TODAY_YYYYMMDD = datetime.now().strftime("%Y%m%d")
TODAY_ISO = datetime.now().strftime("%Y-%m-%d")


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@contextmanager
def without_system_proxy():
    """Temporarily disable proxy environment variables for public data fetches."""
    old_values = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    try:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for key in PROXY_ENV_KEYS:
            if old_values[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_values[key]


@dataclass
class SmokeResult:
    name: str
    output_file: str
    success: bool
    row_count: int
    columns: list[str]
    head_markdown: str
    tail_markdown: str
    error: str
    db_judgment: str


def _markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows returned._"
    return df.to_markdown(index=False)


def _run_case(name: str, output_file: str, fetcher: Callable[[], pd.DataFrame], db_judgment: str) -> SmokeResult:
    output_path = SAMPLE_DIR / output_file
    try:
        with without_system_proxy():
            df = fetcher()
        row_count = len(df)
        columns = [str(col) for col in df.columns]
        if not df.empty:
            df.to_csv(output_path, index=False, encoding="utf-8-sig")

        print(f"\n===== {name} =====")
        print(f"rows={row_count}")
        if "股票列表" in name:
            print(df.head(20).to_string(index=False))
        else:
            print(df.tail(20).to_string(index=False))

        return SmokeResult(
            name=name,
            output_file=str(output_path.relative_to(PROJECT_ROOT)),
            success=True,
            row_count=row_count,
            columns=columns,
            head_markdown=_markdown_table(df.head(5)),
            tail_markdown=_markdown_table(df.tail(5)),
            error="",
            db_judgment=db_judgment,
        )
    except Exception as exc:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        detail = traceback.format_exc()
        print(f"\n===== {name} FAILED =====")
        print(detail)
        return SmokeResult(
            name=name,
            output_file=str(output_path.relative_to(PROJECT_ROOT)),
            success=False,
            row_count=0,
            columns=[],
            head_markdown="_Fetch failed._",
            tail_markdown="_Fetch failed._",
            error=f"{error}\n\n```text\n{detail}\n```",
            db_judgment="读取失败，暂不能判断字段是否适合入库。",
        )


def fetch_akshare_stock_list() -> pd.DataFrame:
    import akshare as ak

    return ak.stock_info_a_code_name()


def fetch_akshare_000001_qfq() -> pd.DataFrame:
    import akshare as ak

    return ak.stock_zh_a_hist(
        symbol="000001",
        period="daily",
        start_date="20240101",
        end_date=TODAY_YYYYMMDD,
        adjust="qfq",
    )


def fetch_akshare_000300_index() -> pd.DataFrame:
    import akshare as ak

    return ak.index_zh_a_hist(symbol="000300", period="daily")


def fetch_baostock_000001() -> pd.DataFrame:
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")

    try:
        rs = bs.query_history_k_data_plus(
            "sz.000001",
            "date,code,open,high,low,close,volume,amount,adjustflag",
            start_date="2024-01-01",
            end_date=TODAY_ISO,
            frequency="d",
            adjustflag="2",
        )
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock query_history_k_data_plus failed: {rs.error_msg}")

        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)
    finally:
        bs.logout()


def _write_report(results: list[SmokeResult]) -> None:
    report_path = SAMPLE_DIR / "smoke_test_report.md"
    lines = [
        "# 免费数据源读取验证报告",
        "",
        f"- 执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 使用数据源：AKShare、BaoStock",
        "- 未使用：Tushare、NewsAPI、任何需要 token/积分/付费的数据源",
        "- 代理策略：脚本运行期间临时禁用 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 等系统代理环境变量",
        "",
        "## 汇总",
        "",
        "| 接口 | 状态 | 返回行数 | 样例文件 | 初步判断 |",
        "| --- | --- | ---: | --- | --- |",
    ]

    for result in results:
        status = "成功" if result.success else "失败"
        lines.append(
            f"| {result.name} | {status} | {result.row_count} | `{result.output_file}` | {result.db_judgment} |"
        )

    for result in results:
        status = "成功" if result.success else "失败"
        lines.extend(
            [
                "",
                f"## {result.name}",
                "",
                f"- 是否成功：{status}",
                f"- 返回行数：{result.row_count}",
                f"- 字段名：`{', '.join(result.columns) if result.columns else '无'}`",
                f"- 样例文件：`{result.output_file}`",
                f"- 初步判断：{result.db_judgment}",
                "",
                "### 前 5 行",
                "",
                result.head_markdown,
                "",
                "### 最近 5 行",
                "",
                result.tail_markdown,
            ]
        )
        if result.error:
            lines.extend(["", "### 错误信息", "", result.error])

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport written: {report_path}")


def main() -> None:
    disable_proxy_if_configured()
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    results = [
        _run_case(
            "AKShare 股票列表 stock_info_a_code_name",
            "stock_list_sample.csv",
            fetch_akshare_stock_list,
            "适合入 stock_basic；需标准化代码后缀、交易所和更新时间字段。",
        ),
        _run_case(
            "AKShare 平安银行前复权日线 stock_zh_a_hist",
            "000001_daily_qfq_sample.csv",
            fetch_akshare_000001_qfq,
            "适合入 adjusted_price；字段需映射为 trade_date/open/high/low/close/vol/amount/source。",
        ),
        _run_case(
            "AKShare 沪深300指数 index_zh_a_hist",
            "000300_index_sample.csv",
            fetch_akshare_000300_index,
            "适合入 index_daily；需补 index_code、source、updated_at。",
        ),
        _run_case(
            "BaoStock 平安银行历史日线 query_history_k_data_plus",
            "000001_baostock_sample.csv",
            fetch_baostock_000001,
            "适合做 daily_price 长周期回填备用源；字段口径与 AKShare 需对齐。",
        ),
    ]
    _write_report(results)


if __name__ == "__main__":
    main()
