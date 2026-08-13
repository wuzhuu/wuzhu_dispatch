from __future__ import annotations

import pandas as pd

from src.analysis.data_loader import StockDataLoader
from src.analysis.factors import calc_latest_factors
from src.analysis.indicators import add_basic_indicators
from src.analysis.risk import add_risk_flags
from src.utils.config import resolve_path


FORBIDDEN_WORDS = ("买入", "卖出", "推荐买入", "满仓", "梭哈", "保证收益")


def build_stock_report(ts_code: str, loader: StockDataLoader) -> dict[str, object]:
    price = loader.get_stock_price(ts_code)
    if price.empty:
        raise RuntimeError(f"No price data found for {ts_code}")
    basic = loader.get_stock_basic()
    info = basic[basic["ts_code"] == ts_code].iloc[0].to_dict() if not basic.empty and (basic["ts_code"] == ts_code).any() else {}
    enriched = add_basic_indicators(price)
    latest = enriched.iloc[-1]
    factors = calc_latest_factors(price)
    risk = add_risk_flags(factors).iloc[0] if not factors.empty else pd.Series(dtype=object)
    rel = _relative_hs300(loader, ts_code, enriched)
    conclusion = _conclusion(latest, risk)
    lines = [
        f"# {ts_code} 个股分析报告",
        "",
        "## 基础信息",
        "",
        f"- 名称：{info.get('name', '')}",
        f"- 行业：{info.get('industry', '')}",
        f"- 市场：{info.get('market', '')}",
        "",
        "## 最新价格",
        "",
        f"- 日期：{latest['trade_date'].date() if hasattr(latest['trade_date'], 'date') else latest['trade_date']}",
        f"- 收盘价：{_fmt(latest.get('close'))}",
        f"- 20日收益率：{_pct(latest.get('ret_20d'))}",
        f"- 60日收益率：{_pct(latest.get('ret_60d'))}",
        f"- 120日收益率：{_pct(latest.get('ret_120d'))}",
        f"- MA20趋势：{'站上' if latest.get('above_ma20') else '未站上'}",
        f"- MA60趋势：{'站上' if latest.get('above_ma60') else '未站上'}",
        f"- 20日波动率：{_pct(latest.get('volatility_20d'))}",
        f"- 60日波动率：{_pct(latest.get('volatility_60d'))}",
        f"- 60日最大回撤：{_pct(latest.get('max_drawdown_60d'))}",
        f"- 20日平均成交额：{_fmt(latest.get('amount_ma_20'))}",
        f"- 相对沪深300 20日表现：{_pct(rel)}",
        "",
        "## 风险标签",
        "",
        f"- 风险等级：{risk.get('risk_level', '')}",
        f"- 风险标签：{risk.get('risk_flags', '')}",
        "",
        "## 结论",
        "",
        conclusion,
    ]
    text = "\n".join(lines) + "\n"
    for word in FORBIDDEN_WORDS:
        text = text.replace(word, "")
    trade_date = latest["trade_date"].date() if hasattr(latest["trade_date"], "date") else latest["trade_date"]
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "report_type": "stock_report",
        "report_version": "v1",
        "report_markdown": text,
        "risk_level": risk.get("risk_level", ""),
        "conclusion": conclusion,
        "created_at": pd.Timestamp.now(),
    }


def generate_stock_report(ts_code: str, loader: StockDataLoader, output_path: str) -> str:
    row = build_stock_report(ts_code, loader)
    path = resolve_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(row["report_markdown"]), encoding="utf-8")
    return str(path)


def _relative_hs300(loader: StockDataLoader, ts_code: str, enriched: pd.DataFrame) -> float | None:
    stock_ret = enriched.iloc[-1].get("ret_20d")
    for code in ("sh.000300", "000300.SH", "000300"):
        idx = loader.get_index_price(code)
        if not idx.empty:
            idx_ret = add_basic_indicators(idx).iloc[-1].get("ret_20d")
            return stock_ret - idx_ret
    return None


def _conclusion(latest: pd.Series, risk: pd.Series) -> str:
    if risk.get("risk_level") == "HIGH":
        return "观察"
    if latest.get("above_ma20") and latest.get("above_ma60") and pd.to_numeric(latest.get("ret_20d"), errors="coerce") > 0:
        return "偏强"
    if not latest.get("above_ma20") and not latest.get("above_ma60"):
        return "偏弱"
    return "中性"


def _fmt(value) -> str:
    return "" if pd.isna(value) else f"{float(value):.4f}"


def _pct(value) -> str:
    return "" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"
