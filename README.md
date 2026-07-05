# hermes-openai-proxy

OpenAI-compatible HTTP API that exposes your **Hermes** credentials to any
client that speaks the OpenAI Chat Completions protocol: Brave Leo BYOM,
GitHub Copilot, Continue, Cline, Aider, Cursor, Open WebUI, etc.

The proxy passes the `messages` array from each request through to the
underlying provider verbatim. It does **not** inject, prepend, append, or
modify any text. It does **not** read `MEMORY.md`, `USER.md`, or any
persona / skills context. What you send in is what the model sees.

## What it does

- Reads API keys from `$HERMES_HOME/.env` (default: `~/.hermes/.env`).
  Any OpenAI-compatible provider you have credentials for is auto-enabled.
- Reads the pinned default model from `$HERMES_HOME/config.yaml`
  (`model.default` and `model.provider`).
- Builds `/v1/models` from a curated allowlist of providers/models,
  filtered to only those with credentials set.
- Accepts `POST /v1/chat/completions` (and bare `/chat/completions`) in
  standard OpenAI JSON shape.
- Returns responses in standard OpenAI shape, including streaming via SSE.
- Routes each request to the underlying provider, translating between
  OpenAI's and Anthropic's native schemas as needed.
- Synthesizes a `data: [DONE]` SSE terminator for providers (MiniMax M3,
  others) that omit it.
- Strips parallel `reasoning_content` and inline `<think>...</think>`
  blocks from thinking-mode responses.
- Translates `response_format: json_schema` to `response_format: json_object`
  with a schema-as-text hint for providers (MiniMax M3, others) that
  only support the older spec.
- Synthesizes a `tools` array from observed tool calls when a client
  (LangChain, Nanobrowser) omits it on subsequent multi-turn turns.
## Quick start

Install directly from the repo (no PyPI release yet — the project is
Windows-tested, macOS/Linux paths coded but unverified):

```bash
pip install git+https://github.com/neostryder/hermes-openai-proxy.git
```

Or, if you want to hack on the proxy itself, clone and install editable:

```bash
git clone https://github.com/neostryder/hermes-openai-proxy.git
cd hermes-openai-proxy
pip install -e ".[dev]"
```

Make sure your Hermes `.env` has at least one provider credential:

```bash
# ~/.hermes/.env
OPENAI_API_KEY=sk-...
# or
ANTHROPIC_API_KEY=sk-ant-...
# or
MINIMAX_API_KEY=...
# etc.
```

(Optional) Pin the default model in `~/.hermes/config.yaml`:

```yaml
model:
  default: MiniMax-M3       # or gpt-4o-mini, claude-sonnet-4-5, ...
  provider: minimax          # or openai, anthropic, ...
```

Start the proxy:

```bash
python -m hermes_openai_proxy
# listening on http://0.0.0.0:8765 by default
```

Override host/port:

```bash
python -m hermes_openai_proxy --host 127.0.0.1 --port 9000
```

Verify it's up:

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/v1/models
```

Send a test request:

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "<provider>/<model>",
    "messages": [{"role":"user","content":"Say pong"}],
    "max_tokens": 20
  }'
```

Streaming:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "<provider>/<model>",
    "messages": [{"role":"user","content":"Count to 5"}],
    "stream": true
  }'
```

## Supported providers

Each provider is enabled automatically when its API key is present in
`~/.hermes/.env`:

| Provider     | Env var                 | Style     | Notes |
|--------------|-------------------------|-----------|-------|
| minimax      | `MINIMAX_API_KEY`       | openai    | MiniMax M-series |
| minimax_cn   | `MINIMAX_CN_API_KEY`    | openai    | China endpoint |
| openai       | `OPENAI_API_KEY`        | openai    | |
| anthropic    | `ANTHROPIC_API_KEY`     | anthropic | Native Anthropic schema, not OpenAI-compat |
| google       | `GEMINI_API_KEY`        | openai    | OpenAI-compat endpoint |
| xai          | `XAI_API_KEY`           | openai    | |
| openrouter   | `OPENROUTER_API_KEY`    | openai    | Routes to hundreds of upstream models |
| kimi         | `KIMI_API_KEY`          | openai    | |
| z.ai / glm   | `GLM_API_KEY`           | openai    | GLM 4.5 / 4.6 |
| deepseek     | `DEEPSEEK_API_KEY`      | openai    | |
| alibaba      | `DASHSCOPE_API_KEY`     | openai    | Qwen via DashScope |
| qwen         | `QWEN_API_KEY`          | openai    | |
| mistral      | `MISTRAL_API_KEY`       | openai    | |
| xiaomi       | `XIAOMI_API_KEY`        | openai    | |
| lmstudio     | `LM_BASE_URL`           | openai    | Local OpenAI-compat (LM Studio, vLLM, llama.cpp HTTP) |
| ollama       | `OLLAMA_BASE_URL`       | openai    | Local OpenAI-compat |

See `.env.example` for every supported env var.

### Adding a new provider

Edit `hermes_openai_proxy/providers.py` and drop a new `Provider()` entry
into the `PROVIDERS` dict. The minimum fields are `name`, `base_url`,
`env_key_var`, `style`, and `models`. Restart the proxy. No other code
changes needed.

If the new provider uses a non-OpenAI native schema (like Anthropic), set
`style="anthropic"` and the proxy will translate the request format
automatically. For OpenAI-compatible endpoints, leave `style="openai"`.

## Wiring up BYOM clients

### Brave Leo

See [`docs/brave-leo-config.md`](docs/brave-leo-config.md) for the full
walkthrough. Short version: in `brave://settings/leo` -> "Add custom
model", set Server Endpoint to `http://127.0.0.1:8765/v1/chat/completions`
and pick any model ID from `/v1/models`.

### GitHub Copilot / Cursor / Cline / Continue / Aider / Open WebUI

Same pattern. Point the client's OpenAI base URL at
`http://127.0.0.1:8765/v1`. Pick a model from `/v1/models`. No API key
is required by the proxy itself (network isolation handles security).

### Nanobrowser (LangChain ChatOpenAI)

Nanobrowser's ChatOpenAI client strips the `/v1` prefix from the base
URL, so set the base URL to `http://127.0.0.1:8765/v1` -- Nanobrowser
will hit `http://127.0.0.1:8765/chat/completions`, which the proxy also
serves. The proxy synthesizes the `tools` array on multi-turn requests
where Nanobrowser omits it, so structured-output agents work without
manual intervention.

## Configuration

The proxy reads its configuration from environment variables and
Hermes's standard config files.

### Environment variables

| Variable                       | Default                | Purpose |
|--------------------------------|------------------------|---------|
| `HERMES_HOME`                  | `~/.hermes`            | Override Hermes's home directory |
| `HERMES_PROXY_HOST`            | `0.0.0.0`              | Bind address |
| `HERMES_PROXY_PORT`            | `8765`                 | Bind port |
| `HERMES_PROXY_LOG_LEVEL`       | `info`                 | `debug`, `info`, `warning`, `error` |
| `HERMES_PROXY_DEBUG_BODIES`    | (unset)                | Set to `1` to dump full request bodies to disk |
| `OPENAI_API_KEY`               | (none)                 | OpenAI credential |
| `ANTHROPIC_API_KEY`            | (none)                 | Anthropic credential |
| ...                            | ...                    | see `.env.example` for the full list |

The CLI flags `--host`, `--port`, `--log-level` override the env vars.

### Hermes `config.yaml`

The proxy reads `model.default` and `model.provider` (and `ask.default_*`
overrides) from `~/.hermes/config.yaml`. If neither is set, the proxy
picks the first credentialled provider's first model. If no credentials
are present, the proxy returns a 400 with an actionable message rather
than guessing wrong.

## Run as a background service

```bash
# Windows (NSSM if admin, else Task Scheduler, else HKCU Run key)
python -m hermes_openai_proxy --install

# macOS (launchd LaunchAgent in your user context)
python -m hermes_openai_proxy --install

# Linux (systemd --user)
python -m hermes_openai_proxy --install

python -m hermes_openai_proxy --status
python -m hermes_openai_proxy --uninstall
```

Logs land in `~/hermes-openai-proxy.log` and `~/hermes-openai-proxy.err.log`
on all platforms.

## Security

The proxy binds to `0.0.0.0:8765` by default and does **not** require
authentication. This is intentional for trusted LAN / Tailscale use.
**Do NOT expose this to the public internet.** Either:

- Bind to a specific interface: `--host 192.168.1.10` or
  `--host 100.x.y.z` (Tailscale).
- Set up a firewall rule (Windows Defender / iptables / pf).
- Run behind Tailscale ACLs.

If you need bearer-token auth, the extension point is in
`server.py`: add a FastAPI dependency that checks
`Authorization: Bearer *** against an env var, then attach it to
the `chat_completions` route. PRs welcome.

## Development

```bash
git clone https://github.com/neostryder/hermes-openai-proxy.git
cd hermes-openai-proxy
pip install -e ".[dev]"

# Run the proxy in dev mode
python -m hermes_openai_proxy --log-level debug

# Run the test suite (requires the proxy to be running)
python tests/test_interactive.py
```

The interactive test suite lives in `tests/test_interactive.py`. It runs
seven categories of checks against a live proxy:

1. `/v1/models` structure
2. each-provider smoke test
3. streaming
4. no-injection contract (Hermes context is never added)
5. multi-turn conversation
6. reasoning-stripped
7. (placeholder for future tests)
8. error cases (validation, malformed JSON, oversized body)

Override the proxy URL via `HERMES_PROXY_URL=http://localhost:9000`.

## Architecture

```
Client (Brave Leo, Aider, etc.)
   |
   | POST /v1/chat/completions  (OpenAI-shaped JSON)
   v
hermes-openai-proxy
   |
   |-- server.py          FastAPI routes, validation, body capture
   |-- config.py          reads $HERMES_HOME/config.yaml for default model
   |-- credentials.py     parses $HERMES_HOME/.env for API keys
   |-- providers.py       provider registry (name, base_url, env_key_var, models)
   |-- reasoning.py       strips <think>...</think> and parallel reasoning
   |
   | Authorization: Bearer ***
   v
Upstream provider (OpenAI, Anthropic, MiniMax, ...)
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow. PRs welcome:

- New providers (add a `Provider()` entry to `providers.py`)
- Better BYOM client support (document quirks in `docs/`)
- Test coverage
- Documentation improvements

## License

MIT. See [`LICENSE`](LICENSE).