from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import pandas as pd
import requests

from src.utils.config import load_api_keys, load_settings
from src.utils.retry import retry_api


NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"


def _get_newsapi_key() -> str:
    api_keys = load_api_keys()
    api_key = api_keys.get("newsapi", {}).get("api_key", "")
    if not api_key:
        raise ValueError("NewsAPI key is empty. Configure stock/key/api_keys.yaml or STOCK_API_KEYS_PATH -> newsapi.api_key.")
    return api_key


def _article_id(url: str, title: str, published_at: str) -> str:
    raw = f"{url}|{title}|{published_at}".encode("utf-8")
    return sha256(raw).hexdigest()


@retry_api()
def _request_newsapi(params: dict[str, Any], api_key: str) -> requests.Response:
    response = requests.get(
        NEWSAPI_EVERYTHING_URL,
        params=params,
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
    response.raise_for_status()
    return response


def fetch_news_articles(target_date: str) -> pd.DataFrame:
    """Fetch market-related news from NewsAPI /v2/everything."""
    settings = load_settings()
    news_cfg: dict[str, Any] = settings.get("newsapi", {})
    if not news_cfg.get("enabled", True):
        return pd.DataFrame()

    api_key = _get_newsapi_key()
    date_obj = datetime.strptime(target_date, "%Y%m%d")
    date_str = date_obj.strftime("%Y-%m-%d")
    query = news_cfg.get("query", "A股 OR 中国股市")
    language = news_cfg.get("language", "zh")
    params = {
        "q": query,
        "from": date_str,
        "to": date_str,
        "language": language,
        "sortBy": news_cfg.get("sort_by", "publishedAt"),
        "pageSize": int(news_cfg.get("page_size", 50)),
        "page": 1,
    }
    response = _request_newsapi(params, api_key)
    payload = response.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {payload.get('code')} {payload.get('message')}")

    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: list[dict[str, Any]] = []
    for article in payload.get("articles", []):
        source = article.get("source") or {}
        url = article.get("url") or ""
        title = article.get("title") or ""
        published_at = article.get("publishedAt") or ""
        rows.append(
            {
                "article_id": _article_id(url, title, published_at),
                "query": query,
                "source_id": source.get("id"),
                "source_name": source.get("name"),
                "author": article.get("author"),
                "title": title,
                "description": article.get("description"),
                "url": url,
                "url_to_image": article.get("urlToImage"),
                "published_at": pd.to_datetime(published_at, errors="coerce", utc=True).tz_localize(None),
                "content": article.get("content"),
                "language": language,
                "fetched_at": fetched_at,
            }
        )
    return pd.DataFrame(rows)
