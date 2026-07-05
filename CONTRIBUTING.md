# Contributing to hermes-openai-proxy

Thanks for your interest in making this proxy work for more providers,
more BYOM clients, and more platforms.

## Quick contribution checklist

1. Fork this repo, branch from `main`.
2. Make your change in a focused commit. Run `python tests/test_interactive.py`
   against a local proxy to confirm no regressions.
3. Update `README.md`, `CHANGELOG.md`, and any relevant doc under `docs/`.
4. Open a PR with a clear description of the change and the test you ran.

## Adding a new provider

1. Drop a `Provider()` entry into the `PROVIDERS` dict in
   `hermes_openai_proxy/providers.py`:
   ```python
   "my_provider": Provider(
       name="my_provider",
       base_url="https://api.my-provider.example/v1",
       env_key_var="MY_PROVIDER_API_KEY",
       style="openai",  # or "anthropic" for the native Anthropic schema
       models=["my-model-1", "my-model-2"],
   ),
   ```
2. Add a credential mapping in `credentials.py`'s `find_credentials()`
   if your provider uses a non-standard env var name.
3. Update `.env.example` with the new env var.
4. Update `README.md`'s provider table.
5. Test by setting the env var and running
   `curl http://127.0.0.1:8765/v1/models` -- your provider's models
   should appear.

## Adding support for a new BYOM client

1. Document the client in `docs/` -- especially its URL conventions,
   any field aliases it sends, and any quirks you found.
2. If the client sends a payload shape the proxy rejects with 422,
   extend `ChatCompletionsRequest` in `server.py` with the missing
   fields (typed `Any` is usually safest for fields with flexible shape).
3. If the client constructs URLs differently from `/v1/chat/completions`
   or `/chat/completions`, add a new `@app.post(...)` alias that calls
   into `chat_completions()`.
4. Add a test case to `tests/test_interactive.py` that replays an
   example payload from the client.

## Code style

- Python 3.10+ syntax.
- Ruff is configured in `pyproject.toml` with `select = ["E", "F", "I",
  "B", "UP", "SIM"]`. Run `ruff check .` before submitting.
- Type hints on public functions.
- Docstrings on non-trivial public functions.

## Testing

The interactive test suite at `tests/test_interactive.py` runs against
a live proxy. Start the proxy first, then run the tests.

```bash
python -m hermes_openai_proxy --log-level info &
python tests/test_interactive.py
```

Most tests require at least one credentialed provider. The bare-path
tests (`/healthz`, validation errors) run without credentials.

## Reporting issues

Use GitHub Issues. Include:

- Proxy version (`python -m hermes_openai_proxy --version`)
- Output of `curl http://127.0.0.1:8765/healthz`
- The full proxy log with `--log-level debug` covering the failure
- The exact client request body (set `HERMES_PROXY_DEBUG_BODIES=1` to
  capture it to disk)
- The client + version (e.g. "Brave 1.74.0", "Aider 0.62.1")

## Code of conduct

This project follows the standard Contributor Covenant. Be kind. Be
precise. Surface disagreements instead of resolving them silently.