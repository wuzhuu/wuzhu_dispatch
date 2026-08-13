from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.utils.config import resolve_path


@dataclass(frozen=True)
class LLMProfileConfig:
    provider: str
    protocol: str
    base_url: str
    endpoint: str
    api_key_env: str
    api_key_file: str
    model: str
    timeout_seconds: int = 90
    max_retries: int = 2
    temperature: float = 0.1
    max_output_tokens: int = 1800
    concurrency: int = 1
    requests_per_minute: int = 20
    mock: bool = False

    @property
    def api_key(self) -> str | None:
        if self.mock:
            return "mock"
        env_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if env_key:
            return env_key.strip()
        if self.api_key_file:
            path = Path(self.api_key_file).expanduser()
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        return None

    @property
    def api_key_source_label(self) -> str:
        sources = []
        if self.api_key_env:
            sources.append(f"env:{self.api_key_env}")
        if self.api_key_file:
            sources.append(f"file:{self.api_key_file}")
        return " or ".join(sources) if sources else "not configured"

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.endpoint.lstrip("/")


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool
    extraction: LLMProfileConfig
    digest: LLMProfileConfig
    fallback_enabled: bool
    use_keyword_rules: bool
    news_llm: dict[str, Any]


def load_llm_config(path: str | Path) -> LLMConfig:
    resolved = resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"LLM config not found: {resolved}")
    with resolved.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    llm = raw.get("llm", {})
    fallback = raw.get("fallback", {})
    return LLMConfig(
        enabled=bool(llm.get("enabled", True)),
        extraction=_profile(raw.get("extraction", {}), resolved.parent),
        digest=_profile(raw.get("digest", {}), resolved.parent),
        fallback_enabled=bool(fallback.get("enabled", True)),
        use_keyword_rules=bool(fallback.get("use_keyword_rules", True)),
        news_llm=dict(raw.get("news_llm", {})),
    )


def _profile(raw: dict[str, Any], config_dir: Path) -> LLMProfileConfig:
    api_key_file = _resolve_optional_file(raw.get("api_key_file"), config_dir)
    return LLMProfileConfig(
        provider=str(raw.get("provider", "")),
        protocol=str(raw.get("protocol", "openai")).lower(),
        base_url=str(raw.get("base_url", "")),
        endpoint=str(raw.get("endpoint", "/chat/completions")),
        api_key_env=str(raw.get("api_key_env", "")),
        api_key_file=api_key_file,
        model=str(raw.get("model", "")),
        timeout_seconds=int(raw.get("timeout_seconds", 90)),
        max_retries=int(raw.get("max_retries", 2)),
        temperature=float(raw.get("temperature", 0.1)),
        max_output_tokens=int(raw.get("max_output_tokens", 1800)),
        concurrency=int(raw.get("concurrency", 1)),
        requests_per_minute=int(raw.get("requests_per_minute", 20)),
        mock=bool(raw.get("mock", False)),
    )


def _resolve_optional_file(value: Any, config_dir: Path) -> str:
    if not value:
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (config_dir / path).resolve(strict=False)
    else:
        path = path.resolve(strict=False)
    return str(path)
