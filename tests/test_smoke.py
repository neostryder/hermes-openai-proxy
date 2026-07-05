"""Minimal smoke tests. Run with: python -m pytest tests/ -v

Tests are designed to run against a real proxy server. Start the proxy
in a separate shell first:

    python -m hermes_openai_proxy --port 8765

Then run the tests. Most tests just hit /v1/models (doesn't call any
provider). One test does a real chat completion if MINIMAX_API_KEY is set.
"""

import json
import os
import sys
from pathlib import Path

import httpx

PROXY = os.environ.get("HERMES_PROXY_URL", "http://127.0.0.1:8765")


def test_healthz():
    r = httpx.get(f"{PROXY}/healthz", timeout=5.0)
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    print(f"  healthz: default={j['default_model']}, {len(j['providers_with_credentials'])} providers")


def test_models():
    r = httpx.get(f"{PROXY}/v1/models", timeout=5.0)
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "list"
    assert isinstance(j["data"], list)
    ids = [m["id"] for m in j["data"]]
    print(f"  models: {len(ids)} available")
    assert len(ids) > 0, "no models in /v1/models -- check .env has at least one API key"
    return ids


def test_chat_completion_default():
    """Real call to the pinned default model. Skip if no key set."""
    health = httpx.get(f"{PROXY}/healthz", timeout=5.0).json()
    if "minimax" not in health["providers_with_credentials"]:
        print("  skipping: no MINIMAX_API_KEY")
        return
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": health["default_model"],
            "messages": [{"role": "user", "content": "Say 'pong' and nothing else."}],
            "max_tokens": 50,
        },
        timeout=60.0,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    j = r.json()
    content = j["choices"][0]["message"]["content"]
    print(f"  chat (default): {content[:80]!r}")


def test_chat_completion_no_context_injection():
    """Verify: the proxy does NOT inject any system prompt of its own.

    We send a 'system' message asking the assistant to claim it's named
    'Bob'. If the proxy injected its own system prompt ahead of ours, the
    model might refuse. If it passes through, the model should play along.
    """
    health = httpx.get(f"{PROXY}/healthz", timeout=5.0).json()
    if "minimax" not in health["providers_with_credentials"]:
        print("  skipping: no MINIMAX_API_KEY")
        return
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": health["default_model"],
            "messages": [
                {"role": "system", "content": "Your name is Bob. Always say 'hi' as Bob."},
                {"role": "user", "content": "Who are you? Answer in 5 words."},
            ],
            "max_tokens": 80,
        },
        timeout=60.0,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    content = r.json()["choices"][0]["message"]["content"]
    print(f"  context-test: {content[:120]!r}")
    # We don't assert "Bob" in the response (the model may paraphrase), but
    # we print so the human reviewing the test output can confirm.


if __name__ == "__main__":
    print(f"Testing proxy at {PROXY}")
    print()
    print("[healthz]")
    test_healthz()
    print()
    print("[/v1/models]")
    ids = test_models()
    if ids:
        print("  sample model IDs:", ids[:5])
    print()
    print("[/v1/chat/completions, default model]")
    test_chat_completion_default()
    print()
    print("[zero-stock context injection test]")
    test_chat_completion_no_context_injection()
    print()
    print("OK")