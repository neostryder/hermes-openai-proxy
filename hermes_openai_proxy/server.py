"""The FastAPI server.

Exposes:
    GET  /healthz                 -> liveness probe
    GET  /v1/models               -> OpenAI-compatible model list (allowlisted + credentialled only)
    POST /v1/chat/completions     -> OpenAI-compatible chat completions
    POST /v1/completions          -> legacy completions, shimmed to chat

CRITICAL: this server passes the client's `messages` array straight through
to the provider. No Hermes context is injected. The client controls
everything in the prompt.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .config import read_default_model
from .credentials import find_credentials, load_env
from .providers import list_available_models, parse_model_id, resolve_provider
from .reasoning import ReasoningFilter, strip_reasoning

log = logging.getLogger("hermes-openai-proxy")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)

app = FastAPI(
    title="hermes-openai-proxy",
    version=__version__,
    description="OpenAI-compatible HTTP API exposing Hermes credentials to any OpenAI-compatible client.",
)

# Permissive CORS. BYOM clients (Brave Leo, Aider, Open WebUI, Continue.dev,
# llama.cpp's web UI) sometimes come from browser contexts. Loopback is also
# expected from local model apps. We allow anything since this server is
# LAN-only and has no built-in auth beyond the bearer token in the request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Boot-time state -------------------------------------------------------

# Filled in at startup from config.yaml + .env. Refreshed on every /v1/models
# request so newly-added credentials show up without a restart.
DEFAULT_MODEL_ID, DEFAULT_PROVIDER = read_default_model()


def _refresh_models() -> list[dict[str, Any]]:
    """Build the OpenAI /v1/models response payload from current creds."""
    creds = find_credentials()
    rows = list_available_models(creds)
    out: list[dict[str, Any]] = []
    for model_id, prov, _ in rows:
        out.append(
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": prov.name,
                "permission": [],
            }
        )
    return out


# -- OpenAI-compatible schemas --------------------------------------------


# Content can be either a plain string (older OpenAI spec) or a list of
# content blocks (newer OpenAI multimodal spec: [{"type": "text", "text": "..."},
# {"type": "image_url", "image_url": {"url": "data:..."}}]). We accept both
# and normalize to a list internally before sending to the upstream provider.
ContentPart = dict[str, Any]


class ChatMessage(BaseModel):
    role: str
    # Accept either str or list of content parts. Pydantic v2 doesn't have
    # a clean Union[str, list] here without model_validator; use Any for
    # flexibility and validate inside the route handler.
    content: Any


class ChatCompletionsRequest(BaseModel):
    model: str = ""  # Allow empty; we fall through to default
    messages: list[ChatMessage] = []  # Allow empty for diagnostic logging
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stream: bool | None = False
    stop: list[str] | None = None
    # OpenAI-compatible tool calling. Brave Leo sends `tools` when the user
    # enables Tool Support in the BYOM panel. Pass through to upstreams
    # that support it (MiniMax M3, OpenAI, etc.); ignore silently otherwise.
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    # OpenAI-compatible structured output. Many clients (LangChain's
    # withStructuredOutput, OpenAI SDK's chat.completions.parse) send
    # `response_format: {type: json_schema, ...}`. We accept it here and
    # translate to {type: json_object} + system-prompt hint before sending
    # to upstreams that don't support json_schema (e.g. MiniMax M3).
    response_format: dict[str, Any] | None = None


class LegacyCompletionsRequest(BaseModel):
    model: str = ""
    prompt: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool | None = False


# Default max_tokens for requests that don't specify one. Must be high enough
# for "thinking-mode" models (MiniMax M3, DeepSeek R1, Qwen QwQ, Kimi k2, GLM
# thinking) to produce visible content after reasoning. A bare "hello" reply
# from MiniMax typically uses 200-400 tokens once reasoning is included.
DEFAULT_MAX_TOKENS = 2048


# -- Upstream payload normalization ----------------------------------------
#
# MiniMax M3 supports OpenAI's legacy `response_format: {"type": "json_object"}`
# but REJECTS the newer `response_format: {"type": "json_schema", ...}` with
# HTTP 400 ("Syntax error"). Several BYOM clients (Nanobrowser via LangChain's
# `withStructuredOutput()`, OpenAI SDK `chat.completions.parse`) send the
# newer shape by default. To keep them working, we transparently translate
# `json_schema` -> `json_object` and inject a schema-as-text hint into the
# system prompt so the model knows what to emit.


def _schema_to_text(schema: dict, depth: int = 0) -> str:
    """Render a JSON Schema object as a compact readable description for
    inclusion in a system prompt. Examples:
        {"type":"object","properties":{"observation":{"type":"string"}}}
        ->  '{ "observation": <string> }'"""
    if depth > 4:  # safety bail-out
        return "<nested>"
    t = schema.get("type", "")
    if t == "object":
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        inner = []
        for k, v in props.items():
            optional = "" if k in required else " (optional)"
            inner.append(f'"{k}": {_schema_to_text(v, depth+1)}{optional}')
        body = ", ".join(inner) if inner else ""
        return "{ " + body + " }"
    if t == "array":
        return f"[{_schema_to_text(schema.get('items', {}), depth+1)}]"
    if t == "string":
        return "<string>"
    if t == "number" or t == "integer":
        return "<number>"
    if t == "boolean":
        return "<true|false>"
    if "anyOf" in schema or "oneOf" in schema:
        # Render as <A | B>
        choices = schema.get("anyOf") or schema.get("oneOf") or []
        return " | ".join(_schema_to_text(c, depth+1) for c in choices)
    if "enum" in schema:
        return "|".join(f'"{e}"' for e in schema["enum"])
    return f"<{t or 'any'}>"


def normalize_response_format(payload: dict) -> None:
    """If payload has response_format.json_schema, translate it to
    response_format.json_object and inject a schema hint into the
    system message. Mutates `payload` in place.

    Specifically:
    - payload["response_format"] -> {"type": "json_object"}
    - For each message in payload["messages"]:
        - If role is system: prepend a one-line schema directive.
        - Otherwise: inject a new system message as the first message.
    """
    rf = payload.get("response_format")
    if not rf or not isinstance(rf, dict):
        return
    schema_obj = rf.get("json_schema")
    if not schema_obj or not isinstance(schema_obj, dict):
        return  # plain json_object: nothing to do
    schema = schema_obj.get("schema") or {}
    schema_text = _schema_to_text(schema)
    schema_hint = (
        "Respond with JSON only (no prose, no markdown fences).\n"
        f"Schema: {schema_text}\n"
        "Example: {}\n"
        "Now produce the JSON for the user's request."
    )

    # Replace response_format with the legacy json_object mode.
    payload["response_format"] = {"type": "json_object"}

    # Inject the hint into messages.
    messages = payload.get("messages") or []
    if messages and isinstance(messages, list) and messages[0].get("role") == "system":
        # Prepend to existing system message.
        existing = messages[0].get("content")
        if isinstance(existing, str):
            messages[0]["content"] = schema_hint + "\n\n" + existing
        elif isinstance(existing, list) and existing and isinstance(existing[0], dict):
            # Content-list form: insert at the start.
            existing.insert(0, {"type": "text", "text": schema_hint})
    else:
        # Insert a new system message at the front.
        payload["messages"] = [{"role": "system", "content": schema_hint}] + messages

    log.info("normalized json_schema -> json_object; schema_hint length=%d", len(schema_hint))


def _synthesize_tools_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """If a request has assistant tool_calls or role:"tool" messages but no
    `tools` array, build a minimal tools array covering every function name
    observed in the conversation. MiniMax M3 and similar providers require
    tool metadata to validate tool_call_id references -- they reject the
    request with HTTP 400 ("tool result's tool id() not found") otherwise.

    Universal fix: works regardless of which client (LangChain,
    Nanobrowser, OpenAI SDK, etc.) decided not to re-send the tools array.

    Two modes are emitted per entry:
    - Default: stub function definition with no id. Some providers use this.
    - Per tool_call_id: a copy of the same function but with an `id` field
      matching each observed tool_call_id in the conversation. MiniMax M3
      matches tool_call_id ↔ tool id, so we emit one entry per id seen.

    Each entry has permissive parameters -- the client already validated
    the schema; we're just satisfying upstream bookkeeping.
    """
    seen_names: dict[str, dict[str, Any]] = {}
    seen_ids: list[str] = []
    for m in messages or []:
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}).get("name")
            tid = tc.get("id")
            if fn and fn not in seen_names:
                seen_names[fn] = {
                    "type": "function",
                    "function": {
                        "name": fn,
                        "description": "Synthesized by hermes-openai-proxy from a prior tool_calls block; original schema not available.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            if tid:
                seen_ids.append(tid)
    # Emit per-id copies for MiniMax's id-based matching, while keeping
    # the name-only base entries for providers that key by name.
    out: list[dict[str, Any]] = list(seen_names.values())
    # Deduplicate ids and cap at 32 to keep the synthesized array bounded
    # for very long multi-turn conversations. MiniMax only needs each id
    # once; duplicates are harmless but waste tokens.
    unique_ids = list(dict.fromkeys(seen_ids))[:32]
    for tid in unique_ids:
        if seen_names:
            base = next(iter(seen_names.values()))
            entry = {"id": tid, **base}
            out.append(entry)
    return out


# -- Routes ----------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Log the raw body + validation errors so we can debug BYOM clients that
    send non-standard payloads. Returns a 422 with the pydantic errors list,
    but ALSO logs the raw body for the operator."""
    try:
        body_bytes = await request.body()
        body_text = body_bytes.decode(errors="ignore")[:4000]
    except Exception as e:
        body_text = f"<could not read body: {e}>"
    log.warning(
        "VALIDATION FAIL: method=%s url=%s body=%s errors=%s",
        request.method,
        request.url.path,
        body_text,
        exc.errors(),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(404)
async def not_found_handler(request: Request, exc) -> JSONResponse:
    """Log 404s so we can spot clients hitting the wrong path."""
    try:
        body_bytes = await request.body()
        body_text = body_bytes.decode(errors="ignore")[:4000]
    except Exception as e:
        body_text = f"<could not read body: {e}>"
    log.warning(
        "404 NOT FOUND: method=%s url=%s headers=%s body=%s",
        request.method,
        request.url.path,
        dict(request.headers),
        body_text,
    )
    return JSONResponse(
        status_code=404,
        content={"detail": f"path {request.url.path} not found. Available: /healthz, /v1/models, /models, /v1/chat/completions, /chat/completions, /v1/completions, /completions"},
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    creds = find_credentials()
    return {
        "status": "ok",
        "version": __version__,
        "default_model": DEFAULT_MODEL_ID,
        "providers_with_credentials": sorted(creds.keys()),
        "models_served": len(_refresh_models()),
    }


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    rows = _refresh_models()
    return {"object": "list", "data": rows}


# Bare-path aliases (no /v1 prefix). Some BYOM clients (Nanobrowser via
# LangChain, certain Chromium extensions) build request URLs by stripping
# the /v1 prefix when given a base URL. Registering the bare paths as
# aliases avoids 404s.
@app.get("/models")
def list_models_alias() -> dict[str, Any]:
    return list_models()


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionsRequest, request: Request) -> Any:
    # ---- DEBUG: log incoming request for Brave BYOM debugging --------
    try:
        body_bytes = await request.body()
        log.info(
            "CHAT request: method=%s url=%s headers=%s body=%s",
            request.method,
            request.url.path,
            dict(request.headers),
            body_bytes[:2000].decode(errors="ignore"),
        )
        # Dump full body to a rotating diagnostic file ONLY when DEBUG_BODIES=1.
        # Disabled by default to avoid filling the disk on long-running sessions.
        if os.environ.get("HERMES_PROXY_DEBUG_BODIES") == "1":
            import time as _t
            _ts = _t.strftime("%Y%m%d_%H%M%S")
            try:
                _diag_dir = Path.home() / "AppData" / "Local" / "hermes-openai-proxy" / "debug-bodies"
                _diag_dir.mkdir(parents=True, exist_ok=True)
                _diag_path = _diag_dir / f"body_{_ts}_{id(request)}.json"
                _diag_path.write_bytes(body_bytes)
                log.info("DEBUG body dumped: %s (%d bytes)", _diag_path, len(body_bytes))
            except Exception as _e:
                log.info("DEBUG body dump failed: %s", _e)
    except Exception as e:
        log.info("CHAT log-failure: %s", e)
    # ---- /DEBUG -------------------------------------------------------

    # 1. Resolve which model/provider to use.
    model_id = req.model or DEFAULT_MODEL_ID
    if model_id == DEFAULT_MODEL_ID or model_id == "":
        model_id = DEFAULT_MODEL_ID

    if not model_id:
        # No default resolved (no config.yaml directive, no credentialled
        # providers). Surface this with an actionable error rather than
        # picking a model that might not exist for this user.
        raise HTTPException(
            status_code=400,
            detail=(
                "no default model resolved. Either set model.default + "
                "model.provider in $HERMES_HOME/config.yaml, or add at "
                "least one API key to $HERMES_HOME/.env. See README.md "
                "for the full setup walkthrough."
            ),
        )

    # 2. Parse 'provider/model' -> (provider, model)
    provider_name, bare_model = parse_model_id(model_id)
    if not provider_name:
        # client sent just a model name with no provider prefix
        provider_name = DEFAULT_PROVIDER or ""
        bare_model = model_id

    # 3. Resolve provider and check credentials
    env = load_env()
    try:
        prov = resolve_provider(provider_name, env)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    api_key = env.get(prov.env_key_var or "", "").strip() if prov.env_key_var else ""
    if not api_key and prov.name not in ("lmstudio", "ollama"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"no credentials for provider '{prov.name}'. Set "
                f"{prov.env_key_var} in ~/.hermes/.env"
            ),
        )

    # 4. Build the messages array we'll send upstream.
    # CRITICAL: pass the client's messages through verbatim. No injection.
    # Normalize content: clients send either a string OR a list of content
    # blocks (Brave Leo uses the list form). Pass through either as-is.
    # We rebuild the messages list directly from the raw body so that
    # Pydantic's strict model doesn't drop fields like tool_calls,
    # tool_call_id, name, refusal, audio, etc. that some clients (LangChain,
    # Nanobrowser, OpenAI Python SDK) include on assistant and tool messages.
    # Fall back to the parsed model if the raw body isn't a valid JSON object
    # with a `messages` key (this happens when chat_completions is invoked
    # internally by the legacy /v1/completions shim).
    upstream_messages: list[dict[str, Any]] = []
    try:
        raw_body_obj = json.loads(body_bytes) if body_bytes else None
        if isinstance(raw_body_obj, dict):
            upstream_messages = raw_body_obj.get("messages") or []
    except Exception:
        upstream_messages = []
    if not upstream_messages:
        # Fall back to the pydantic-parsed messages. This handles the
        # internal call from legacy_completions (which constructs a
        # ChatCompletionsRequest with messages and calls chat_completions
        # directly -- no fresh request body exists).
        for m in req.messages:
            d = m.model_dump()
            if d.get("content") is None:
                d["content"] = ""
            upstream_messages.append(d)
    if not upstream_messages:
        raise HTTPException(
            status_code=400,
            detail="messages array is empty -- a chat completion requires at least one message",
        )

    # 5. Per-provider shape.
    if prov.style == "anthropic":
        if req.stream:
            return StreamingResponse(
                _stream_anthropic(
                    prov, api_key, bare_model, upstream_messages, req
                ),
                media_type="text/event-stream",
            )
        return await _call_anthropic(prov, api_key, bare_model, upstream_messages, req)
    # default: OpenAI-compatible
    if req.stream:
        return StreamingResponse(
            _stream_openai_compat(prov, api_key, bare_model, upstream_messages, req),
            media_type="text/event-stream",
        )
    return await _call_openai_compat(prov, api_key, bare_model, upstream_messages, req)


@app.post("/v1/completions")
async def legacy_completions(req: LegacyCompletionsRequest, request: Request) -> Any:
    """Shim the legacy /v1/completions to /v1/chat/completions with a
    single user message. Some clients (older Aider, etc.) only speak
    completions."""
    chat_req = ChatCompletionsRequest(
        model=req.model,
        messages=[ChatMessage(role="user", content=req.prompt)],
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        stream=req.stream,
    )
    return await chat_completions(chat_req, request)


# Bare-path aliases (no /v1 prefix). Some BYOM clients (Nanobrowser via
# LangChain, certain Chromium extensions) build request URLs by stripping
# the /v1 prefix when given a base URL. Registering the bare paths as
# aliases avoids 404s.
@app.post("/chat/completions")
async def chat_completions_alias(req: ChatCompletionsRequest, request: Request) -> Any:
    return await chat_completions(req, request)


@app.post("/completions")
async def legacy_completions_alias(req: LegacyCompletionsRequest, request: Request) -> Any:
    return await legacy_completions(req, request)


# -- Helpers ---------------------------------------------------------------


async def _err_chunk_streaming(
    completion_id: str, created: int, model: str, error_type: str, message: str
):
    """Yield a single SSE error chunk followed by [DONE]. Used by streaming
    handlers to surface upstream connection/timeout failures to the client."""
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
        "error": {"type": error_type, "message": message},
    }
    yield f"data: {json.dumps(chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


# -- Anthropic path --------------------------------------------------------


async def _call_anthropic(
    prov, api_key, model, messages, req: ChatCompletionsRequest
) -> dict[str, Any]:
    url = f"{prov.base_url}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    out_msgs: list[dict[str, str]] = []
    sys_block: str | None = None
    for m in messages:
        if m["role"] == "system":
            sys_block = m["content"]
        else:
            out_msgs.append(m)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
        "messages": out_msgs,
    }
    if sys_block:
        payload["system"] = sys_block
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        try:
            r = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503,
                detail=f"anthropic unreachable at {prov.base_url}: {str(e)[:200]}. "
                       "Is the anthropic server running?",
            ) from e
        except httpx.TimeoutException as e:
            raise HTTPException(
                status_code=504,
                detail=f"anthropic timed out: {str(e)[:200]}",
            ) from e
    if r.status_code in (401, 403):
        raise HTTPException(status_code=401, detail=f"anthropic auth: {r.text[:200]}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    j = r.json()
    text_parts = [b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"]
    text = "\n".join(text_parts)
    usage = j.get("usage", {})
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": j.get("model", model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": strip_reasoning(text)},
                "finish_reason": j.get("stop_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


async def _stream_anthropic(
    prov, api_key, model, messages, req: ChatCompletionsRequest
) -> AsyncIterator[bytes]:
    url = f"{prov.base_url}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    out_msgs: list[dict[str, str]] = []
    sys_block: str | None = None
    for m in messages:
        if m["role"] == "system":
            sys_block = m["content"]
        else:
            out_msgs.append(m)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": req.max_tokens or DEFAULT_MAX_TOKENS,
        "messages": out_msgs,
        "stream": True,
    }
    if sys_block:
        payload["system"] = sys_block
    if req.temperature is not None:
        payload["temperature"] = req.temperature

    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    filter_state = ReasoningFilter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client, \
                client.stream("POST", url, headers=headers, json=payload) as r:
                log.info("UPSTREAM %s -> %d (stream)", prov.name, r.status_code)
                if r.status_code >= 400:
                    body = await r.aread()
                    log.warning("UPSTREAM %s body: %s", prov.name, body.decode(errors="ignore")[:400])
                    err_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                        "error": {"status": r.status_code, "body": body.decode(errors="ignore")[:400]},
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {}).get("text", "")
                        visible = filter_state.feed(delta) if delta else ""
                        if visible:
                            chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {"index": 0, "delta": {"content": visible}, "finish_reason": None}
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n".encode()
                    elif ev.get("type") == "message_stop":
                        tail = filter_state.flush()
                        if tail:
                            tail_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {"index": 0, "delta": {"content": tail}, "finish_reason": None}
                                ],
                            }
                            yield f"data: {json.dumps(tail_chunk)}\n\n".encode()
                        end_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        }
                        yield f"data: {json.dumps(end_chunk)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
    except httpx.ConnectError as e:
        async for chunk in _err_chunk_streaming(
            completion_id, created, model, "connect_error",
            f"anthropic unreachable at {prov.base_url}: {str(e)[:200]}",
        ):
            yield chunk
    except httpx.TimeoutException as e:
        async for chunk in _err_chunk_streaming(
            completion_id, created, model, "timeout",
            f"anthropic timed out: {str(e)[:200]}",
        ):
            yield chunk


# -- OpenAI-compat path ----------------------------------------------------


async def _call_openai_compat(
    prov, api_key, model, messages, req: ChatCompletionsRequest
) -> dict[str, Any]:
    url = f"{prov.base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens
    else:
        # OpenAI-compat providers don't require max_tokens, but thinking-mode
        # ones (MiniMax M3) need it or they may go over budget thinking.
        payload["max_tokens"] = DEFAULT_MAX_TOKENS
    if req.top_p is not None:
        payload["top_p"] = req.top_p
    if req.stop is not None:
        payload["stop"] = req.stop
    if req.tools:
        payload["tools"] = req.tools
    else:
        # Universal fix for clients (e.g. LangChain's withStructuredOutput,
        # Nanobrowser Planner/Navigator) that include assistant tool_calls
        # and role:"tool" messages in the conversation but omit the `tools`
        # array on subsequent turns. MiniMax M3 rejects these with HTTP 400
        # "tool result's tool id() not found". Synthesize a tools array from
        # the function names observed in the conversation so the upstream
        # validator has metadata for every tool_call_id referenced.
        synthesized = _synthesize_tools_from_messages(payload.get("messages", []))
        if synthesized:
            payload["tools"] = synthesized
            log.info("synthesized tools from conversation: %s", [t["function"]["name"] for t in synthesized])
    if req.tool_choice is not None:
        payload["tool_choice"] = req.tool_choice
    if req.response_format is not None:
        payload["response_format"] = req.response_format
        normalize_response_format(payload)
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        try:
            r = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503,
                detail=f"{prov.name} unreachable at {prov.base_url}: {str(e)[:200]}. "
                       f"Is the {prov.name} server running?",
            ) from e
        except httpx.TimeoutException as e:
            raise HTTPException(
                status_code=504,
                detail=f"{prov.name} timed out: {str(e)[:200]}",
            ) from e
    log.info("UPSTREAM %s -> %d", prov.name, r.status_code)
    if r.status_code in (401, 403):
        raise HTTPException(status_code=401, detail=f"{prov.name} auth: {r.text[:200]}")
    if r.status_code >= 400:
        log.warning("UPSTREAM %s body: %s", prov.name, r.text[:400])
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    j = r.json()
    # If the upstream returned an OpenAI-shaped response, sanitize:
    # strip inline reasoning blocks from content, drop parallel `reasoning` field.
    if "choices" in j and isinstance(j["choices"], list) and j["choices"]:
        for ch in j["choices"]:
            msg = ch.get("message") or {}
            if "content" in msg and msg["content"]:
                msg["content"] = strip_reasoning(msg["content"])
            msg.pop("reasoning", None)
            msg.pop("reasoning_content", None)
        j.setdefault("model", model)
        return j
    raise HTTPException(
        status_code=502,
        detail=f"upstream returned non-OpenAI-shaped response: {str(j)[:300]}",
    )


async def _stream_openai_compat(
    prov, api_key, model, messages, req: ChatCompletionsRequest
) -> AsyncIterator[bytes]:
    url = f"{prov.base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens
    else:
        # OpenAI-compat providers don't require max_tokens, but thinking-mode
        # ones (MiniMax M3) need it or they may go over budget thinking.
        payload["max_tokens"] = DEFAULT_MAX_TOKENS
    if req.top_p is not None:
        payload["top_p"] = req.top_p
    if req.stop is not None:
        payload["stop"] = req.stop
    if req.tools:
        payload["tools"] = req.tools
    else:
        # Universal fix for clients (e.g. LangChain's withStructuredOutput,
        # Nanobrowser Planner/Navigator) that include assistant tool_calls
        # and role:"tool" messages in the conversation but omit the `tools`
        # array on subsequent turns. MiniMax M3 rejects these with HTTP 400
        # "tool result's tool id() not found". Synthesize a tools array from
        # the function names observed in the conversation so the upstream
        # validator has metadata for every tool_call_id referenced.
        synthesized = _synthesize_tools_from_messages(payload.get("messages", []))
        if synthesized:
            payload["tools"] = synthesized
            log.info("synthesized tools from conversation: %s", [t["function"]["name"] for t in synthesized])
    if req.tool_choice is not None:
        payload["tool_choice"] = req.tool_choice
    if req.response_format is not None:
        payload["response_format"] = req.response_format
        normalize_response_format(payload)

    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    filter_state = ReasoningFilter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client, \
                client.stream("POST", url, headers=headers, json=payload) as r:
                log.info("UPSTREAM %s -> %d (stream)", prov.name, r.status_code)
                if r.status_code >= 400:
                    body = await r.aread()
                    log.warning("UPSTREAM %s body: %s", prov.name, body.decode(errors="ignore")[:400])
                    err_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                        "error": {"status": r.status_code, "body": body.decode(errors="ignore")[:400]},
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        # Flush any remaining visible text from the filter.
                        tail = filter_state.flush()
                        if tail:
                            tail_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {"index": 0, "delta": {"content": tail}, "finish_reason": None}
                                ],
                            }
                            yield f"data: {json.dumps(tail_chunk)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    # Rewrite to ensure id/object are set if upstream omitted them
                    ev.setdefault("id", completion_id)
                    ev.setdefault("object", "chat.completion.chunk")
                    ev.setdefault("created", created)
                    ev.setdefault("model", model)
                    # Walk choices; for each delta, drop the parallel reasoning
                    # field and run delta.content through the reasoning filter.
                    choices = ev.get("choices") or []
                    if not choices:
                        yield f"data: {json.dumps(ev)}\n\n".encode()
                        continue
                    emitted_any = False
                    new_choices = []
                    saw_finish_reason = False
                    for ch in choices:
                        delta = ch.get("delta") or {}
                        # Drop parallel reasoning field if upstream includes it
                        # (MiniMax M3 streaming does this).
                        filter_state.consume_reasoning_field(delta.pop("reasoning", ""))
                        filter_state.consume_reasoning_field(delta.pop("reasoning_content", ""))
                        raw_content = delta.get("content", "") or ""
                        visible = filter_state.feed(raw_content)
                        if visible:
                            # Emit a chunk with just the visible part.
                            new_delta = dict(delta)
                            new_delta["content"] = visible
                            # Remove other content-like fields if present to avoid duplicates
                            new_delta.pop("reasoning", None)
                            new_delta.pop("reasoning_content", None)
                            new_choices.append({
                                "index": ch.get("index", 0),
                                "delta": new_delta,
                                "finish_reason": ch.get("finish_reason"),
                            })
                            emitted_any = True
                        elif ch.get("finish_reason"):
                            # Pass through the finish_reason chunk even if no visible content.
                            new_choices.append({
                                "index": ch.get("index", 0),
                                "delta": {},
                                "finish_reason": ch.get("finish_reason"),
                            })
                            emitted_any = True
                        # Track finish_reason even when content also came through in
                        # this same chunk. Some upstreams (notably MiniMax M3/M2.7
                        # streaming) emit a single chunk containing BOTH `delta.content`
                        # AND `choices[0].finish_reason: "stop"`; without this,
                        # saw_finish_reason would stay False and we'd never
                        # synthesize the [DONE] terminator on line 944, leaving
                        # well-behaved clients (curl, OpenAI SDK) hanging.
                        if ch.get("finish_reason"):
                            saw_finish_reason = True
                    if emitted_any:
                        ev["choices"] = new_choices
                        yield f"data: {json.dumps(ev)}\n\n".encode()
                    # If the upstream marked the stream finished but didn't send
                    # [DONE] (some providers, notably MiniMax M3, skip it),
                    # synthesize the terminator ourselves so clients don't hang.
                    if saw_finish_reason:
                        tail = filter_state.flush()
                        if tail:
                            tail_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {"index": 0, "delta": {"content": tail}, "finish_reason": None}
                                ],
                            }
                            yield f"data: {json.dumps(tail_chunk)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
    except httpx.ConnectError as e:
        async for chunk in _err_chunk_streaming(
            completion_id, created, model, "connect_error",
            f"{prov.name} unreachable at {prov.base_url}: {str(e)[:200]}",
        ):
            yield chunk
    except httpx.TimeoutException as e:
        async for chunk in _err_chunk_streaming(
            completion_id, created, model, "timeout",
            f"{prov.name} timed out: {str(e)[:200]}",
        ):
            yield chunk