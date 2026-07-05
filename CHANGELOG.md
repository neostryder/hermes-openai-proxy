# Changelog

All notable changes to hermes-openai-proxy are documented here. Versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Streaming `[DONE]` synthesis when upstream sends a single chunk with
  both content and `finish_reason: "stop"`.** MiniMax M2.7 (and sometimes
  M3) emit a single SSE chunk at end-of-stream containing BOTH
  `choices[0].delta.content` and `choices[0].finish_reason: "stop"`.
  The streaming filter had an `if visible: ... elif finish_reason: ...`
  chain that only set `saw_finish_reason` in the `elif` branch. When the
  chunk had both, the `elif` was skipped, the `[DONE]` terminator was
  never synthesized, and well-behaved clients (curl, OpenAI SDK,
  LangChain) hung until they hit their own timeout. Discovered during
  the first install on gamemaster (macOS, Apple Silicon, Darwin 25.5.0),
  where rpgm's MiniMax-M2.7 doesn't emit a trailing `[DONE]`. Eru's
  MiniMax-M3 emits it correctly, which masked the bug locally. Fix:
  track `saw_finish_reason` independently of the content branch.

### Changed

- Test fixture `tests/test_interactive.py`: drop explicit `max_tokens` in
  tests 5 (multi-turn) and 6 (reasoning-stripped). Thinking-mode
  models (MiniMax M3 / M2.7) sometimes consume the entire budget on
  reasoning, leaving zero tokens for visible content. Letting the
  upstream set the default produces reliable results across both
  reasoning tiers.

### Added

- `MANIFEST.in` to ensure source distribution includes README, LICENSE,
  CHANGELOG.md, .env.example, examples/, docs/, and tests/. Previous
  installs via `pip install git+...` did not include these files; tests
  had to be transferred manually.
- `python-dotenv>=1.0` to `requirements.txt` (was previously a
  transitive dependency that came in via starlette, but every fresh
  install relies on it -- declaring it explicitly makes the intent
  clear).

## [0.1.0] - 2026-07-05

### Added

- FastAPI server exposing `/v1/chat/completions`, `/v1/completions`,
  `/v1/models`, `/healthz` (plus bare-path aliases `/chat/completions`,
  `/models`, `/completions`).
- Auto-discovery of provider credentials from `~/.hermes/.env`.
- Provider registry covering MiniMax (incl. CN endpoint), OpenAI,
  Anthropic, Google Gemini, xAI Grok, OpenRouter, Moonshot Kimi,
  Z.ai GLM, DeepSeek, Alibaba DashScope, Qwen, Mistral, Xiaomi MiMo,
  Novita, KiloCode, OpenCode Zen/Go, plus local servers (LM Studio,
  Ollama, vLLM, llama.cpp HTTP) via `LM_BASE_URL` / `OLLAMA_BASE_URL`.
- Anthropic native-schema translation (requests/responses converted
  between OpenAI and Anthropic formats on the fly).
- Streaming SSE responses with synthesized `data: [DONE]` terminator
  for upstreams that omit it (MiniMax M3).
- Reasoning-block stripper for inline `<think>...</think>` and parallel
  `reasoning_content` channels (MiniMax M3, DeepSeek R1, Kimi K2,
  GLM thinking, etc.).
- Structured-output normalization: `response_format: json_schema` is
  translated to `response_format: json_object` with a schema-as-text
  hint injected into the system prompt, making LangChain's
  `withStructuredOutput()` and Nanobrowser's Planner/Navigator work
  transparently against providers that only support the older spec.
- Tool-call id synthesizer: when a multi-turn request omits the
  `tools` array but the conversation references `tool_call_id`s, the
  proxy synthesizes a stub `tools` entry per observed function name
  plus a per-id copy keyed on each `tool_call_id`. Critical fix for
  Nanobrowser Planner/Navigator.
- Raw-body preservation: `messages` array is rebuilt from the raw
  request body so Pydantic doesn't strip fields like `tool_calls`,
  `tool_call_id`, `name`, `refusal` that strict models don't declare.
- CORS middleware for browser-based clients (Brave Leo, Nanobrowser).
- Diagnostic body dump (`HERMES_PROXY_DEBUG_BODIES=1`) writes full
  request bodies to `~/AppData/Local/hermes-openai-proxy/debug-bodies/`
  for offline inspection of BYOM quirks.
- Cross-platform service installer: `--install` / `--uninstall` /
  `--status` commands handle NSSM, Task Scheduler, HKCU Run key on
  Windows; launchd LaunchAgent on macOS; systemd --user on Linux.
- Auto-enable GitHub Copilot via `COPILOT_GITHUB_TOKEN`.
- 7/7 interactive test suite covering models endpoint, multi-provider
  smoke, streaming, no-injection contract, multi-turn, reasoning
  stripping, and error cases.
- Documentation: README, brave-leo-config walkthrough, CONTRIBUTING,
  this CHANGELOG, .env.example, LICENSE (MIT).