from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.llm.client import LLMClient
from src.llm.config import load_llm_config
from src.llm.exceptions import LLMError
from src.llm.prompts import NEWS_EXTRACTION_SYSTEM_PROMPT


def main() -> None:
    parser = argparse.ArgumentParser(description="Test configured LLM provider without writing news analysis results.")
    parser.add_argument("--config", default="../key/stackAnalys_llm.yaml")
    parser.add_argument("--profile", default="extraction", choices=["extraction", "digest"])
    args = parser.parse_args()
    try:
        cfg = load_llm_config(args.config)
    except FileNotFoundError as exc:
        print(f"WARNING: {exc}")
        print("recommended: copy ../config/llm.example.yaml to ../key/stackAnalys_llm.yaml or ~/key/stackAnalys_llm.yaml, then set api_key_file outside stackAnalys.")
        return
    profile = getattr(cfg, args.profile)
    print(f"profile: {args.profile}")
    print(f"provider: {profile.provider}")
    print(f"protocol: {profile.protocol}")
    print(f"model: {profile.model}")
    print(f"api_key_env: {profile.api_key_env}")
    print(f"api_key_file: {profile.api_key_file or ''}")
    if not profile.api_key and not profile.mock:
        print(f"WARNING: missing API key from {profile.api_key_source_label}")
        return
    client = LLMClient(profile)
    health = client.health_check()
    print(f"health_status: {health['status']}")
    print(f"latency_ms: {health['latency_ms']}")
    if health["status"] not in ("OK",):
        print(f"health_error: {health['error']}")
    try:
        payload, response = client.chat_json(
            NEWS_EXTRACTION_SYSTEM_PROMPT,
            "返回一个最小 JSON：{\"is_financial_news\": false, \"event_type\": \"other\", \"topics\": [], \"sentiment\": \"neutral\", \"sentiment_score\": 0, \"impact_direction\": \"uncertain\", \"impact_horizon\": \"uncertain\", \"importance_score\": 0, \"market_relevance\": 0, \"risk_level\": \"UNKNOWN\", \"industries\": [], \"stocks\": [], \"organizations\": [], \"countries_regions\": [], \"summary\": \"provider test\", \"key_points\": [], \"evidence\": [], \"uncertainty\": \"test\", \"confidence\": 0.5}",
        )
        print("json_status: OK")
        print(f"response_keys: {sorted(payload.keys())}")
        print(f"response_latency_ms: {response.latency_ms}")
    except LLMError as exc:
        print(f"json_status: {exc.error_type.value}")
        print(f"error: {exc.message}")


if __name__ == "__main__":
    main()
