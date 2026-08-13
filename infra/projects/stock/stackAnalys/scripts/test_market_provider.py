from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.market_provider import MarketDataProvider
from src.utils.proxy import disable_proxy_if_configured


SAMPLE_DIR = PROJECT_ROOT / "data" / "samples"
REPORT_PATH = SAMPLE_DIR / "market_provider_test.md"
START_DATE = "2024-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")


def _markdown_table(df: pd.DataFrame) -> str:
    return "_No rows returned._" if df.empty else df.to_markdown(index=False)


def _run_case(provider: MarketDataProvider, name: str, fetcher) -> dict[str, object]:
    try:
        df = fetcher()
        info = provider.last_run
        source = info.source if info else "none"
        fallback = bool(info.fallback_used) if info else False
        error = f"primary={info.primary_error}; fallback={info.fallback_error}" if info and (info.primary_error or info.fallback_error) else ""
        print(f"\n===== {name} =====")
        print(f"source={source}")
        print(f"rows={len(df)}")
        print(f"fallback={fallback}")
        print("前5行:")
        print(df.head(5).to_string(index=False))
        print("最近5行:")
        print(df.tail(5).to_string(index=False))
        return {"name": name, "success": True, "source": source, "rows": len(df), "fallback": fallback, "error": error, "df": df}
    except Exception as exc:
        info = provider.last_run
        error = repr(exc)
        if info:
            error += f"; primary={info.primary_error}; fallback={info.fallback_error}"
        print(f"\n===== {name} FAILED =====")
        print(error)
        return {"name": name, "success": False, "source": "failed", "rows": 0, "fallback": bool(info.fallback_used) if info else False, "error": error, "df": pd.DataFrame()}


def _write_report(results: list[dict[str, object]]) -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# MarketDataProvider 测试报告",
        "",
        f"- 执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 策略：BaoStock 主源，AKShare 备用源",
        "",
        "## 汇总",
        "",
        "| 数据 | 是否成功 | 实际数据源 | 返回行数 | 是否 fallback | 错误 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r['name']} | {'成功' if r['success'] else '失败'} | {r['source']} | {r['rows']} | {r['fallback']} | {r['error']} |")
    for r in results:
        df = r["df"]
        lines.extend(["", f"## {r['name']}", "", f"- 实际使用的数据源：{r['source']}", f"- 返回行数：{r['rows']}", f"- 是否发生 fallback：{r['fallback']}", f"- 错误信息：{r['error']}", "", "### 前5行", "", _markdown_table(df.head(5)), "", "### 最近5行", "", _markdown_table(df.tail(5))])
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport written: {REPORT_PATH}")


def main() -> None:
    disable_proxy_if_configured()
    provider = MarketDataProvider()
    cases = [
        ("平安银行 sz.000001", lambda: provider.get_daily_price("sz.000001", START_DATE, END_DATE)),
        ("浦发银行 sh.600000", lambda: provider.get_daily_price("sh.600000", START_DATE, END_DATE)),
        ("宁德时代 sz.300750", lambda: provider.get_daily_price("sz.300750", START_DATE, END_DATE)),
        ("沪深300 sh.000300", lambda: provider.get_index_daily("sh.000300", START_DATE, END_DATE)),
    ]
    results = [_run_case(provider, name, fetcher) for name, fetcher in cases]
    _write_report(results)


if __name__ == "__main__":
    main()
