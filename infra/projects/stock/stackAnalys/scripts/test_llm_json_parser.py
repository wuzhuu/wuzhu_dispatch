from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.llm.client import _loads_json_object


def main() -> None:
    cases = [
        ('{"market_summary": "ok"}', {"market_summary": "ok"}),
        ('```json\n{"market_summary": "ok"}\n```', {"market_summary": "ok"}),
        ('说明文字\n{"market_summary": "ok", "items": [1, 2]}\n后缀', {"market_summary": "ok", "items": [1, 2]}),
        ('```json\n{"market_summary": "ok"}\n```\n补充说明', {"market_summary": "ok"}),
    ]
    for raw, expected in cases:
        actual = _loads_json_object(raw)
        assert actual == expected, (raw, actual)

    for raw in ("[]", "no json"):
        try:
            _loads_json_object(raw)
        except ValueError:
            continue
        except Exception:
            continue
        raise AssertionError(f"expected parser failure for {raw!r}")

    print("llm_json_parser: OK")


if __name__ == "__main__":
    main()
