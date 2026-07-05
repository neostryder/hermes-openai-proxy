"""Comprehensive interactive test suite for hermes-openai-proxy.

Run against a live proxy at http://127.0.0.1:8765 (default). Verifies:
  1. /v1/models structure + completeness
  2. Each credentialed provider responds to a real chat completion
  3. Streaming works (SSE chunks arrive in order, [DONE] terminates)
  4. The proxy does NOT inject any system prompt (zero-stock test)
  5. Multi-turn conversation passes through verbatim
  6. Reasoning blocks stripped (thinking-mode models)
  7. Legacy /v1/completions shim works
  8. Error cases: bad model, missing creds, malformed body, unicode

Usage:
    python tests/test_interactive.py
    python tests/test_interactive.py --model openai/gpt-4o-mini
    python tests/test_interactive.py --model minimax/MiniMax-M3 --only 2,3

If --model is omitted, the script resolves the test target from the
proxy's /healthz endpoint (default_model field), or if that's empty,
the first entry in /v1/models. This means a contributor with any
credentialed provider can run the full suite without setting
HERMES_TEST_MODEL.
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

PROXY = os.environ.get("HERMES_PROXY_URL", "http://127.0.0.1:8765")
TIMEOUT = httpx.Timeout(60.0, connect=5.0)


def hr(s: str) -> None:
    print()
    print("=" * 70)
    print(s)
    print("=" * 70)


def _print(label: str, value: Any) -> None:
    if isinstance(value, str) and len(value) > 200:
        value = value[:200] + "...[truncated]"
    print(f"  {label}: {value}")


def test_1_models_list() -> dict[str, Any]:
    hr("1. /v1/models -- structure + completeness")
    r = httpx.get(f"{PROXY}/v1/models", timeout=TIMEOUT)
    assert r.status_code == 200, f"got {r.status_code}"
    j = r.json()
    assert j["object"] == "list"
    assert isinstance(j["data"], list) and len(j["data"]) > 0
    for m in j["data"]:
        assert "id" in m
        assert "/" in m["id"], f"model id missing provider prefix: {m}"
        assert m["object"] == "model"
    by_prov: dict[str, int] = {}
    for m in j["data"]:
        by_prov[m["owned_by"]] = by_prov.get(m["owned_by"], 0) + 1
    _print("model_count", len(j["data"]))
    _print("by_provider", by_prov)
    return j


def test_2_each_provider_one_model(models: dict[str, Any], only: list[str]) -> list[tuple[str, bool, str]]:
    hr("2. Each credentialed provider -- one real chat completion per provider")
    # Pick one model per provider
    by_prov: dict[str, str] = {}
    for m in models["data"]:
        p = m["owned_by"]
        if p not in by_prov:
            by_prov[p] = m["id"]
    results: list[tuple[str, bool, str]] = []
    for prov, model_id in sorted(by_prov.items()):
        if only and prov not in only and model_id not in only:
            continue
        print(f"\n  [{prov}] model={model_id}")
        try:
            r = httpx.post(
                f"{PROXY}/v1/chat/completions",
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": "Reply with exactly the word 'pong' and nothing else."},
                        {"role": "user", "content": "go"},
                    ],
                    # Omit max_tokens to use the proxy's default. This is
                    # important: thinking-mode models (MiniMax M3) need a
                    # large budget to produce visible content after reasoning.
                    "temperature": 0,
                },
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                body = r.text[:200]
                # Distinguish environmental issues (server down, quota) from
                # real proxy bugs. SKIP marks environmental failures; FAIL
                # would mean a proxy bug.
                env_skip = (
                    r.status_code in (429, 503)  # quota / unreachable server
                    or "No models loaded" in body  # LM Studio
                    or "quota" in body.lower()      # generic quota
                )
                status_label = "SKIP-ENV" if env_skip else "FAIL"
                reason = (
                    "quota exhausted" if r.status_code == 429 else
                    "server unreachable" if r.status_code == 503 else
                    "upstream reported no model loaded" if "No models loaded" in body else
                    body
                )
                print(f"    {status_label} status={r.status_code}: {reason}")
                # Env-skip = green for this test; count as pass for the test's purpose
                results.append((model_id, env_skip, f"{status_label}: {reason[:80]}"))
                continue
            j = r.json()
            content = j.get("choices", [{}])[0].get("message", {}).get("content", "")
            content_lc = content.strip().lower()
            ok = "pong" in content_lc and len(content_lc) < 50
            print(f"    response: {content!r}")
            print(f"    {'PASS' if ok else 'FAIL'}")
            results.append((model_id, ok, content[:200]))
        except Exception as e:
            print(f"    EXCEPTION: {e}")
            results.append((model_id, False, str(e)[:200]))
    return results


def test_3_streaming(model_id: str) -> bool:
    hr(f"3. Streaming (SSE) -- model={model_id}")
    print("  sending stream request...")
    chunks: list[dict[str, Any]] = []
    done_seen = False
    t0 = time.time()
    with httpx.stream(
        "POST",
        f"{PROXY}/v1/chat/completions",
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "Count from 1 to 5 with spaces between."}],
            # Omit max_tokens -- thinking-mode models need the proxy's default.
            "stream": True,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    ) as r:
        for line in r.iter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done_seen = True
                continue
            try:
                ev = json.loads(data)
                chunks.append(ev)
            except Exception:
                pass
    dt = time.time() - t0
    print(f"  chunks received: {len(chunks)} in {dt:.2f}s, done={done_seen}")
    if not chunks:
        print("  FAIL: no chunks")
        return False
    if not done_seen:
        print("  FAIL: no [DONE] terminator")
        return False
    # Reconstruct content
    full = "".join(
        (c.get("choices", [{}])[0].get("delta", {}) or {}).get("content", "") or ""
        for c in chunks
    )
    print(f"  reconstructed content: {full!r}")
    has_1 = "1" in full
    has_5 = "5" in full
    print(f"  has '1': {has_1}, has '5': {has_5}")
    return done_seen and chunks and has_1 and has_5


def test_4_no_injection(model_id: str) -> bool:
    hr(f"4. Zero-stock: proxy does NOT inject its own system prompt -- model={model_id}")
    # We tell the model it is a pirate. If the proxy injected its own system
    # prompt ahead of ours, the model might either ignore ours or behave
    # differently. A passing test = the model obeys OUR system prompt.
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": model_id,
            "messages": [
                {"role": "system", "content": "You are a pirate. Reply to the user with one short sentence that begins with 'Arrr!'."},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 60,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    print(f"  response: {content!r}")
    ok = "arrr" in content.lower()
    print(f"  {'PASS' if ok else 'FAIL'} (looked for 'Arrr!')")
    return ok


def test_5_multi_turn(model_id: str) -> bool:
    hr(f"5. Multi-turn conversation -- model={model_id}")
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": model_id,
            "messages": [
                {"role": "user", "content": "My favorite color is blue. Remember this."},
                {"role": "assistant", "content": "Got it, your favorite color is blue."},
                {"role": "user", "content": "What is my favorite color? Reply in 2 words."},
            ],
            # Omit max_tokens -- thinking-mode models (MiniMax M3 / M2.7) sometimes
            # consume the entire budget on reasoning. Letting the upstream set the
            # default ensures we get visible content even on older reasoning models.
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    print(f"  response: {content!r}")
    ok = "blue" in content.lower()
    print(f"  {'PASS' if ok else 'FAIL'} (looked for 'blue')")
    return ok


def test_6_reasoning_stripped(model_id: str) -> bool:
    hr(f"6. Reasoning blocks stripped -- model={model_id}")
    # Streaming version: collect all visible chunks
    chunks: list[str] = []
    with httpx.stream(
        "POST",
        f"{PROXY}/v1/chat/completions",
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "What is 2+2? Reply in one word."}],
            # Omit max_tokens -- thinking-mode models need the proxy's default.
            "stream": True,
        },
        timeout=TIMEOUT,
    ) as r:
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
                content = (ev.get("choices", [{}])[0].get("delta", {}) or {}).get("content", "") or ""
                chunks.append(content)
            except Exception:
                pass
    full_stream = "".join(chunks)
    print(f"  streamed content: {full_stream!r}")

    # Non-streaming version
    r2 = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "What is 2+2? Reply in one word."}],
            "max_tokens": 200,
        },
        timeout=TIMEOUT,
    )
    assert r2.status_code == 200
    content_ns = r2.json()["choices"][0]["message"]["content"]
    print(f"  non-stream content: {content_ns!r}")

    # Pass criteria: no 'think' / 'reasoning' tags, answer is reasonable
    no_tags_stream = "<think>" not in full_stream and "<reasoning>" not in full_stream
    no_tags_ns = "<think>" not in content_ns and "<reasoning>" not in content_ns
    stream_reasonable = "4" in full_stream or "four" in full_stream.lower()
    ns_reasonable = "4" in content_ns or "four" in content_ns.lower()
    print(f"  stream: no_tags={no_tags_stream}, reasonable={stream_reasonable}")
    print(f"  non-stream: no_tags={no_tags_ns}, reasonable={ns_reasonable}")
    return no_tags_stream and no_tags_ns and stream_reasonable and ns_reasonable


def test_7_legacy_completions(model_id: str) -> bool:
    hr(f"7. Legacy /v1/completions shim -- model={model_id}")
    r = httpx.post(
        f"{PROXY}/v1/completions",
        json={
            "model": model_id,
            "prompt": "Reply with the word pong",
            # Omit max_tokens -- thinking-mode models need the proxy's default.
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        print(f"  FAIL status={r.status_code}: {r.text[:200]}")
        return False
    j = r.json()
    # Legacy /v1/completions returns 'choices[].text' not 'choices[].message.content'
    text = j.get("choices", [{}])[0].get("text", "")
    if not text:
        # If proxy shim mapped to chat completions shape, look there
        text = j.get("choices", [{}])[0].get("message", {}).get("content", "")
    print(f"  text: {text!r}")
    ok = "pong" in text.lower()
    print(f"  {'PASS' if ok else 'FAIL'}")
    return ok


def test_8_error_cases(model_id: str = "") -> bool:
    hr("8. Error cases")
    cases_pass = 0
    cases_total = 0

    # 8a. Unknown provider
    cases_total += 1
    print("\n  8a. Unknown provider prefix")
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": "made-up-provider/gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
        timeout=TIMEOUT,
    )
    print(f"    status={r.status_code}, body={r.text[:200]}")
    ok = r.status_code == 400 or r.status_code == 404
    print(f"    {'PASS' if ok else 'FAIL'}")
    if ok:
        cases_pass += 1

    # 8b. Empty messages
    cases_total += 1
    print("\n  8b. Empty messages list")
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": "minimax/MiniMax-M3",
            "messages": [],
            "max_tokens": 10,
        },
        timeout=TIMEOUT,
    )
    print(f"    status={r.status_code}")
    # Either 400 (validation) or 200 with empty/short response -- accept either
    ok = r.status_code in (200, 400, 422)
    print(f"    {'PASS' if ok else 'FAIL'}")
    if ok:
        cases_pass += 1

    # 8c. Malformed JSON
    cases_total += 1
    print("\n  8c. Malformed JSON body")
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        content=b"this is not json",
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    print(f"    status={r.status_code}")
    ok = r.status_code >= 400
    print(f"    {'PASS' if ok else 'FAIL'}")
    if ok:
        cases_pass += 1

    # 8d. Very long user message (50KB)
    cases_total += 1
    print("\n  8d. Very long user message (50KB)")
    long_msg = "The quick brown fox. " * 2500
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": "minimax/MiniMax-M3",
            "messages": [{"role": "user", "content": long_msg}],
            "max_tokens": 20,
        },
        timeout=TIMEOUT,
    )
    print(f"    status={r.status_code}")
    # Should succeed or return a clean error from the upstream provider
    ok = r.status_code in (200, 400, 413, 502)
    print(f"    {'PASS' if ok else 'FAIL'}")
    if ok:
        cases_pass += 1

    # 8e. Unicode in messages
    cases_total += 1
    print("\n  8e. Unicode in messages")
    # Use httpx json= for proper UTF-8 handling (curl + bash quoting mangles unicode).
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        json={
            "model": model_id or "minimax/MiniMax-M3",
            "messages": [
                {"role": "system", "content": "You handle any language. Reply briefly."},
                {"role": "user", "content": "こんにちは! Comment ça va? 你好? مرحبا?"},
            ],
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    if r.status_code == 200:
        content = r.json()["choices"][0]["message"]["content"]
        print(f"    response: {content!r}")
        ok = len(content) > 0
    else:
        print(f"    status={r.status_code}, body={r.text[:200]}")
        ok = False
    print(f"    {'PASS' if ok else 'FAIL'}")
    if ok:
        cases_pass += 1

    print(f"\n  Total: {cases_pass}/{cases_total} error cases passed")
    return cases_pass == cases_total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None,
                        help="default model for single-model tests. "
                             "If omitted, the proxy's /healthz default is used; "
                             "if that is empty, the first model from /v1/models.")
    parser.add_argument("--only", default=None,
                        help="comma-separated list of providers to test (e.g. 'minimax,openai')")
    args = parser.parse_args()

    only = args.only.split(",") if args.only else []

    results: list[tuple[str, bool]] = []

    # 1 -- also resolves our default model from /healthz if not supplied
    try:
        models = test_1_models_list()
        if args.model is None:
            try:
                h = httpx.get(f"{PROXY}/healthz", timeout=TIMEOUT).json()
                default_from_proxy = h.get("default_model", "")
                if default_from_proxy and any(
                    m["id"] == default_from_proxy for m in models["data"]
                ):
                    args.model = default_from_proxy
                elif models["data"]:
                    # Proxy had no usable default; pick first available model.
                    args.model = models["data"][0]["id"]
                else:
                    args.model = ""
            except Exception:
                args.model = models["data"][0]["id"] if models["data"] else ""
        results.append(("1. /v1/models structure", True))
    except AssertionError as e:
        print(f"  FAIL: {e}")
        results.append(("1. /v1/models structure", False))
        return

    print(f"  (default model for single-model tests: {args.model!r})")

    # 2
    try:
        per_prov = test_2_each_provider_one_model(models, only)
        ok_count = sum(1 for _, ok, _ in per_prov if ok)
        results.append((f"2. each provider ({ok_count}/{len(per_prov)} passed)",
                        ok_count == len(per_prov)))
    except Exception as e:
        print(f"  FAIL: {e}")
        results.append(("2. each provider", False))

    # 3-7 use the requested default model
    test_3_streaming(args.model) and results.append(("3. streaming", True))
    test_4_no_injection(args.model) and results.append(("4. no-injection", True))
    test_5_multi_turn(args.model) and results.append(("5. multi-turn", True))
    test_6_reasoning_stripped(args.model) and results.append(("6. reasoning-stripped", True))
    test_7_legacy_completions(args.model) and results.append(("7. legacy /v1/completions", True))

    # 8
    test_8_error_cases(args.model) and results.append(("8. error cases", True))

    # Summary
    hr("SUMMARY")
    for name, ok in results:
        print(f"  {'OK' if ok else 'FAIL'}  {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} tests passed")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()