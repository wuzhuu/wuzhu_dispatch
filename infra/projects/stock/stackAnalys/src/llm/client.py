from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests

from src.llm.config import LLMProfileConfig
from src.llm.exceptions import LLMError, LLMErrorType


@dataclass
class LLMResponse:
    request_id: str
    content: str
    raw_response: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    retry_count: int


class LLMClient:
    def __init__(self, profile: LLMProfileConfig):
        self.profile = profile
        self._last_request_at = 0.0

    def chat_json(self, system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], LLMResponse]:
        response = self.chat_text(system_prompt, user_prompt, response_format={"type": "json_object"})
        try:
            return _loads_json_object(response.content), response
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMError(LLMErrorType.INVALID_JSON, f"Invalid JSON response: {exc}") from exc

    def chat_text(self, system_prompt: str, user_prompt: str, response_format: dict[str, Any] | None = None) -> LLMResponse:
        if self.profile.mock:
            return self._mock_response(system_prompt, user_prompt)
        api_key = self.profile.api_key
        if not api_key:
            raise LLMError(LLMErrorType.AUTH_ERROR, f"Missing API key from {self.profile.api_key_source_label}")
        payload = self._payload(system_prompt, user_prompt, response_format)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        last_error: LLMError | None = None
        for attempt in range(self.profile.max_retries + 1):
            self._rate_limit()
            started = time.monotonic()
            try:
                resp = requests.post(self.profile.url, headers=headers, json=payload, timeout=self.profile.timeout_seconds)
            except requests.Timeout as exc:
                last_error = LLMError(LLMErrorType.TIMEOUT, str(exc))
                if attempt < self.profile.max_retries:
                    time.sleep(1 + attempt)
                    continue
                raise last_error from exc
            except requests.RequestException as exc:
                last_error = LLMError(LLMErrorType.PROVIDER_ERROR, str(exc))
                if attempt < self.profile.max_retries:
                    time.sleep(1 + attempt)
                    continue
                raise last_error from exc
            latency_ms = int((time.monotonic() - started) * 1000)
            if resp.status_code in (401, 403):
                raise LLMError(LLMErrorType.AUTH_ERROR, "Provider authentication failed", resp.status_code)
            if resp.status_code == 429:
                last_error = LLMError(LLMErrorType.RATE_LIMIT, "Provider rate limit", resp.status_code)
                if attempt < self.profile.max_retries:
                    time.sleep(2 + attempt)
                    continue
                raise last_error
            if resp.status_code >= 400:
                raise LLMError(LLMErrorType.PROVIDER_ERROR, f"Provider HTTP {resp.status_code}", resp.status_code)
            data = resp.json()
            content = self._extract_content(data)
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            return LLMResponse(
                request_id=str(uuid.uuid4()),
                content=content,
                raw_response=json.dumps(data, ensure_ascii=False),
                input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
                latency_ms=latency_ms,
                retry_count=attempt,
            )
        raise last_error or LLMError(LLMErrorType.PROVIDER_ERROR, "Provider request failed")

    def list_models(self) -> dict[str, Any]:
        if self.profile.mock:
            return {"data": [{"id": self.profile.model}]}
        api_key = self.profile.api_key
        if not api_key:
            raise LLMError(LLMErrorType.AUTH_ERROR, f"Missing API key from {self.profile.api_key_source_label}")
        url = self.profile.base_url.rstrip("/") + "/models"
        resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=min(self.profile.timeout_seconds, 30))
        if resp.status_code in (401, 403):
            raise LLMError(LLMErrorType.AUTH_ERROR, "Provider authentication failed", resp.status_code)
        if resp.status_code >= 400:
            raise LLMError(LLMErrorType.PROVIDER_ERROR, f"Provider HTTP {resp.status_code}", resp.status_code)
        return resp.json()

    def health_check(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            models = self.list_models()
            status = "OK"
            error = ""
        except LLMError as exc:
            models = {}
            status = exc.error_type.value
            error = exc.message
        return {
            "provider": self.profile.provider,
            "protocol": self.profile.protocol,
            "model": self.profile.model,
            "status": status,
            "error": error,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "models_seen": len(models.get("data", [])) if isinstance(models, dict) else 0,
        }

    def _payload(self, system_prompt: str, user_prompt: str, response_format: dict[str, Any] | None) -> dict[str, Any]:
        if self.profile.protocol == "anthropic":
            return {
                "model": self.profile.model,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
                "temperature": self.profile.temperature,
                "max_tokens": self.profile.max_output_tokens,
            }
        payload = {
            "model": self.profile.model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": self.profile.temperature,
            "max_tokens": self.profile.max_output_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        return payload

    def _extract_content(self, data: dict[str, Any]) -> str:
        if self.profile.protocol == "anthropic":
            content = data.get("content", [])
            if content and isinstance(content, list):
                return str(content[0].get("text", ""))
        choices = data.get("choices", [])
        if choices:
            return str(choices[0].get("message", {}).get("content", ""))
        raise LLMError(LLMErrorType.PROVIDER_ERROR, "Provider response has no content")

    def _rate_limit(self) -> None:
        rpm = max(int(self.profile.requests_per_minute or 0), 1)
        interval = 60.0 / rpm
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at = time.monotonic()

    def _mock_response(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        industries = []
        stocks = []
        topics = ["mock"]
        if "银行" in user_prompt:
            industries.append({"name": "银行", "relation": "mentioned", "confidence": 0.8, "evidence": "银行"})
            stocks.append({"name": "平安银行", "relation": "mentioned", "confidence": 0.7, "evidence": "银行"})
            topics.append("bank_finance")
        if "半导体" in user_prompt or "芯片" in user_prompt:
            industries.append({"name": "半导体", "relation": "mentioned", "confidence": 0.8, "evidence": "半导体"})
            topics.append("semiconductor")
        payload = {
            "is_financial_news": True,
            "event_type": "other",
            "topics": topics,
            "sentiment": "neutral",
            "sentiment_score": 0.0,
            "impact_direction": "uncertain",
            "impact_horizon": "uncertain",
            "importance_score": 50,
            "market_relevance": 50,
            "risk_level": "UNKNOWN",
            "industries": industries,
            "stocks": stocks,
            "organizations": [],
            "countries_regions": [],
            "summary": "mock analysis",
            "key_points": [],
            "evidence": ["mock evidence"],
            "uncertainty": "mock mode",
            "confidence": 0.5,
        }
        text = json.dumps(payload, ensure_ascii=False)
        return LLMResponse(str(uuid.uuid4()), text, text, 0, 0, 0, 0)


def _loads_json_object(text: str) -> dict[str, Any]:
    stripped = _strip_code_fence(text)
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(stripped)
        if stripped[end:].strip():
            raise json.JSONDecodeError("Extra data", stripped, end)
    except json.JSONDecodeError:
        payload = _extract_first_json_object(stripped, decoder)
    if not isinstance(payload, dict):
        raise ValueError("JSON response must be an object")
    return payload


def _extract_first_json_object(text: str, decoder: json.JSONDecoder) -> Any:
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise json.JSONDecodeError("No JSON object found", text, 0)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()
