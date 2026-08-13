from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.utils.config import load_settings


@dataclass(frozen=True)
class FreeSource:
    name: str
    category: str
    enabled: bool
    description: str
    requires_token: bool = False


def list_free_sources() -> list[FreeSource]:
    settings = load_settings()
    cfg: dict[str, Any] = settings.get("free_sources", {})
    ak_cfg = cfg.get("akshare", {})
    providers = ak_cfg.get("providers", {})
    sources = [
        FreeSource("akshare", "aggregator", bool(ak_cfg.get("enabled", True)), "Free A-share data aggregator; no token required."),
        FreeSource("eastmoney", "market/news", bool(providers.get("eastmoney", True)), "Eastmoney endpoints exposed through AKShare."),
        FreeSource("sina", "market/news", bool(providers.get("sina", True)), "Sina Finance endpoints exposed through AKShare/RSS."),
        FreeSource("tencent", "market", bool(providers.get("tencent", True)), "Tencent quote endpoints exposed through AKShare."),
        FreeSource("netease", "market", bool(providers.get("netease", True)), "NetEase quote/history endpoints exposed through AKShare."),
        FreeSource("baostock", "market/fundamental", bool(cfg.get("baostock", {}).get("enabled", True)), "BaoStock free login-based A-share data; no token required."),
        FreeSource("yahoo_finance", "market", bool(cfg.get("yahoo_finance", {}).get("enabled", True)), "Yahoo Finance via yfinance; useful for .SS/.SZ symbols."),
        FreeSource("stooq", "market", bool(cfg.get("stooq", {}).get("enabled", True)), "Stooq CSV endpoint; useful as lightweight global quote source."),
        FreeSource("rss_news", "news", bool(cfg.get("rss_news", {}).get("enabled", True)), "Free RSS feeds configured in settings.yaml."),
    ]
    skillhub_cfg: dict[str, Any] = cfg.get("skillhub", {})
    skillhub_descriptions = {
        "westock_data": ("market/derived", "SkillHub westock-data CLI; candidate source for board ranks, hot stocks, LHB, ETF and IPO calendar."),
        "a_stock_analysis": ("market/intraday", "SkillHub a-stock-analysis script; candidate source for A-share spot quote and intraday volume analysis."),
        "aihot": ("news", "AI HOT public API; candidate source for Chinese AI industry news."),
        "news_summary": ("news", "International RSS aggregation skill; candidate source for global business and technology headlines."),
        "equal_data": ("market/fundamental", "Equal Data API; candidate paid/keyed source for fundamentals and event data."),
    }
    for name, (category, default_description) in skillhub_descriptions.items():
        source_cfg = skillhub_cfg.get(name, {})
        sources.append(
            FreeSource(
                name,
                category,
                bool(source_cfg.get("enabled", False)),
                str(source_cfg.get("description", default_description)),
                requires_token=bool(source_cfg.get("requires_token", name == "equal_data")),
            )
        )
    return sources
