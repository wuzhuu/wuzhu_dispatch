from __future__ import annotations

from tenacity import retry, stop_after_attempt, wait_fixed


def retry_api(max_retry: int = 3, wait_seconds: int = 5):
    return retry(stop=stop_after_attempt(max_retry), wait=wait_fixed(wait_seconds), reraise=True)
