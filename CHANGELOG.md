# Changelog

All notable changes to hermes-openai-proxy are documented here. Versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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