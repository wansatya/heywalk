"""Live smoke test for the Meta Model API integration (AGENTS.md section 6).

Sends one minimal Chat Completions request to verify the model identifier,
base URL, and API key before building on the full workflow.

Usage (from services/api):
    MODEL_API_KEY=[redacted] python scripts/smoke_model.py

Environment:
    MODEL_API_KEY      required; never printed, never logged.
    META_API_BASE_URL  default https://api.meta.ai/v1
    MUSE_MODEL         default muse-spark-1.3
    MODEL_TIMEOUT_S    default 60 for this script

Exit codes: 0 = live model reachable, 1 = request failed, 2 = not configured.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

BASE_URL = os.environ.get("META_API_BASE_URL", "https://api.meta.ai/v1").rstrip(
    "/"
)
MODEL = os.environ.get("MUSE_MODEL", "muse-spark-1.3")
TIMEOUT_S = float(os.environ.get("MODEL_TIMEOUT_S", "60"))
API_KEY = os.environ.get("MODEL_API_KEY", "")


def main() -> int:
    if not API_KEY:
        print(
            "NOT CONFIGURED: set MODEL_API_KEY on the server to run the live"
            " smoke test.",
            file=sys.stderr,
        )
        return 2
    url = BASE_URL + "/chat/completions"
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "temperature": 0,
        "max_completion_tokens": 16,
    }
    started = time.monotonic()
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": "Bearer [redacted]",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=TIMEOUT_S,
        )
    except httpx.TimeoutException:
        print(f"FAIL: request timed out after {TIMEOUT_S:g}s.", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(
            f"FAIL: {type(exc).__name__} calling {BASE_URL}.", file=sys.stderr
        )
        return 1
    latency_ms = int((time.monotonic() - started) * 1000)
    if response.status_code != 200:
        print(
            f"FAIL: HTTP {response.status_code}:"
            f" {response.text[:300].strip() or '(empty body)'}",
            file=sys.stderr,
        )
        return 1
    try:
        text = (
            response.json()["choices"][0]["message"]["content"] or ""
        ).strip()
    except (ValueError, KeyError, IndexError, TypeError):
        print("FAIL: response had no usable choices[0].message.content.")
        return 1
    print(f"OK: live model {MODEL!r} via {BASE_URL} in {latency_ms} ms.")
    print(f"model reply: {text[:200]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
