Brave Leo BYOM configuration for hermes-openai-proxy
====================================================

This guide shows how to point Brave Leo's "Bring Your Own Model" (BYOM)
panel at the hermes-openai-proxy running on your machine. Once configured,
Leo will route every chat through your local proxy, which will in turn
talk to whichever LLM provider(s) you have credentials for.

How to add a custom model
-------------------------

1. Open Brave.
2. Go to `brave://settings/leo` (Leo settings).
3. Click "Add custom model" (button text varies by Brave version).
4. Fill in the fields below.
5. Save and select the new model as the active one.

Field values
------------

  Model Request Name:  <your-model-id>
  Server Endpoint:     http://127.0.0.1:8765/v1/chat/completions
  Context Size:        32000
  API Key:             local
  Vision Support:      OFF
  Tool Support:        OFF
  System Prompt:       <optional -- your choice>

Field semantics (confirmed from Brave source: brave-core/components/ai_chat/):

- **Model Request Name**: literal string sent in the `model` field of
  the chat-completions call (see `oai_api_client.cc::CreateJSONRequestBody`).
  Use any model ID that the proxy's `/v1/models` endpoint exposes --
  e.g. `minimax/MiniMax-M3`, `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-5`.
  Bare names without `/` fall through to the proxy's pinned default
  (set via `model.default` + `model.provider` in your Hermes `config.yaml`).
- **Server Endpoint**: POSTed to VERBATIM (no path appending -- see
  `oai_api_client.cc::PerformRequest`). So this must be the FULL chat
  completions URL, not just a base. Brave's URL validator
  (`model_validator.cc::IsValidEndpoint`) accepts `http://localhost` or
  `http://127.0.0.1` (loopback) OR `https://` URLs.
- **Context Size**: tokens. Brave passes this as
  `max_associated_content_length` for page summarization.
- **API Key**: included as `Authorization: Bearer ***` header (or
  omitted if empty). The proxy does not enforce auth on this LAN-only
  service -- the API key field is essentially decorative. Type any
  non-empty string if your version of Leo requires it.
- **Vision Support / Tool Support**: must match what the upstream model
  actually supports. If you turn Tool Support ON, Brave will send a
  `tools` array in the request body and expect a `role: "tool"`
  conversation roundtrip -- the proxy preserves those messages
  verbatim.
- **Temperature is HARDCODED to 0.7 by Brave** -- there is no setting
  in the BYOM panel to change it. If you need a different temperature,
  use a different client (or proxy the temperature yourself).

URL paths accepted by the proxy
--------------------------------

The proxy accepts both `/v1`-prefixed and bare paths so it works with
clients that strip the `/v1` prefix:

  GET  /v1/models           |  GET  /models
  POST /v1/chat/completions |  POST /chat/completions
  POST /v1/completions      |  POST /completions
  GET  /healthz

Clients that strip `/v1` (use the bare path form):

- Nanobrowser (LangChain ChatOpenAI) -- requires `/chat/completions`
- Some Chromium extensions -- also use `/chat/completions`

Brave Leo uses `/v1/chat/completions` (correct path).

What's actually happening on the wire
-------------------------------------

Leo sends an OpenAI-shaped POST to `${endpoint}/chat/completions`:

  {
    "model": "<your-model-id>",
    "messages": [
      {"role": "system", "content": [
        {"type": "text", "text": "<prompt above, with %datetime% substituted>"}
      ]},
      {"role": "user", "content": [
        {"type": "text", "text": "<your question>"}
      ]}
    ],
    "max_tokens": 32000,
    "stream": true,
    "temperature": 0.7,
    "tools": [
      {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    ]
  }

Notes:

- The `content` field is a LIST of content parts, not a plain string.
  This is the OpenAI multimodal spec; OpenAI, Anthropic via the
  OpenAI-compat endpoint, MiniMax M3, and most modern providers all
  accept this format. Older OpenAI clients sent `content: "..."` as a
  plain string. The proxy accepts both shapes.
- If you enabled Tool Support in the BYOM panel, Leo will also include
  a `tools` array describing Brave's built-in browser tools (code
  execution, memory storage, etc.). The proxy passes these through to
  upstreams that support tool calling (OpenAI, Anthropic, MiniMax M3,
  Gemini).
- The proxy preserves the messages array verbatim with NO Hermes
  context injection. Whatever you put in the System Prompt field above
  is exactly what the upstream model sees.
- The proxy strips reasoning blocks (`<think>...</think>`) from the
  response so Leo's chat UI never accidentally shows chain-of-thought.

Troubleshooting
---------------

- "Network error" / Leo times out: confirm
  `curl http://127.0.0.1:8765/healthz` returns 200 OK from a terminal
  on the same machine. If it doesn't, the proxy is not running; see
  "Starting the proxy" in README.md.
- Leo gets generic canned responses: the API key field is empty or the
  endpoint has a trailing slash. Inspect the Network tab in DevTools --
  the request URL should end in `/v1/chat/completions`.
- Leo's responses include visible `<think>` blocks: the proxy is not
  being used (Brave is talking directly to its own backend). Re-check
  that the custom model is the SELECTED model, not just configured.
- 422 "Validation error" from Leo: Brave is sending a payload shape
  the proxy doesn't expect. Run the proxy with `--log-level debug` to
  capture the raw body, then open an issue with the body shape and the
  Brave version.

Quick test from the terminal
----------------------------

Sanity-check before opening Brave:

  curl http://127.0.0.1:8765/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
      "model": "<your-model-id>",
      "messages": [
        {"role": "system", "content": "Reply briefly."},
        {"role": "user", "content": "What is the meaning of life?"}
      ],
      "temperature": 0
    }'

If this returns a sensible answer but Leo does not, the issue is in
Brave's UI settings, not the proxy.

Customising the System Prompt
-----------------------------

Drop any prompt text into the System Prompt field. If you want the
current date baked in, Brave substitutes the literal `%datetime%` token
at request time. Replace it with a fixed date and the prompt will
freeze at that moment.

The hermes-openai-proxy does not impose any system prompt. Whatever
you type in Leo's System Prompt field is what the upstream model sees
-- nothing is prepended, appended, or modified.

The proxy also does NOT read MEMORY.md, USER.md, or any persona /
skills context from your Hermes install. If you want a specific
persona (say, "you are Lt. Commander Data"), put the persona
description in Leo's System Prompt field, not in a Hermes skill.